from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kama_claude.core.bus.envelope import INVALID_PARAMS, HandlerError
from kama_claude.core.bus.events import (
    SessionClosedEvent,
    SessionCreatedEvent,
    SessionMessageReceivedEvent,
    SessionResumedEvent,
    SessionWaitingForInputEvent,
    SkillInvokedEvent,
)
from kama_claude.core.events.bus import EventBus
from kama_claude.core.runs import new_run_id
from kama_claude.core.sandbox.errors import SandboxPolicyError
from kama_claude.core.sandbox.policy import normalize_path
from kama_claude.core.session.model import Session, SessionMode
from kama_claude.core.session.store import SessionStore
from kama_claude.core.skills.loader import SkillLoader

if TYPE_CHECKING:
    from kama_claude.core.llm.base import LLMProvider
    from kama_claude.core.runner import AgentRunner
    from kama_claude.core.subagent.registry import AgentRegistry, BackgroundTaskRegistry

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012
SESSION_IN_USE = -32013
SESSION_PROJECT_MISMATCH = -32014
SESSION_NOT_RESUMABLE = -32015
SESSION_INVALID = -32016


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionManager:
    # 初始化会话管理器，接入文件存储、runner 工厂、事件总线和可选的 LLM provider（用于手动压缩）
    def __init__(
        self,
        store: SessionStore,
        runner_factory: Callable[[], AgentRunner],
        bus: EventBus,
        provider: LLMProvider | None = None,
        workspace: Path | None = None,
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._workspace = (workspace or Path.cwd()).resolve()
        self._background_tasks: dict[str, list[BackgroundTaskRegistry]] = {}
        self._registries: dict[str, AgentRegistry] = {}
        self._stopping = False

    # 退出开始后拒绝创建会话和启动新的执行
    def stop_accepting(self) -> None:
        self._stopping = True

    # 防止退出清理期间重新创建注册表或接收输入
    def _check_accepting(self) -> None:
        if self._stopping:
            raise HandlerError(SESSION_CLOSED, "core is shutting down")

    # 离开前重试主历史提交，失败时保留运行期上下文供再次重试
    def _flush_main(self, sid: str) -> None:
        registry = self._registries.get(sid)
        if registry is not None:
            main = registry.ensure_main()
            self._store.save_agent(main.context, main.latest_run_id or "flush")

    # 返回会话的稳定 agent 注册表，恢复时仅加载主历史
    def agent_registry(self, sid: str) -> AgentRegistry:
        from kama_claude.core.subagent.registry import AgentRegistry

        self._check_accepting()
        session = self._get_session(sid)
        if session.status == "closed":
            raise HandlerError(SESSION_CLOSED, "session already closed")
        if sid not in self._registries:
            registry = AgentRegistry(sid, self._bus)
            registry.ensure_main(self._store.read_messages(sid))
            self._registries[sid] = registry
        return self._registries[sid]

    # 会话离开时关闭子树并释放运行期索引
    async def _release_agents(self, sid: str) -> None:
        registry = self._registries.pop(sid, None)
        if registry is not None:
            await registry.shutdown()
        self._background_tasks.pop(sid, None)

    # Core 退出前等待全部子树清理完成
    async def shutdown(self) -> None:
        self.stop_accepting()
        for sid in list(self._registries):
            await self._release_agents(sid)

    # 比较规范化项目路径，旧记录未保存项目时允许用户显式绑定
    def _matches_workspace(self, session: Session, workspace: Path) -> bool:
        return session.workspace is None or os.path.normcase(
            str(Path(session.workspace).resolve())
        ) == os.path.normcase(str(workspace))

    # 客户端传绝对启动目录；仅旧客户端缺省时沿用 Core 目录，不修改进程 cwd
    def _workspace_for(self, workspace: Path | None) -> Path:
        candidate = workspace if workspace is not None else self._workspace
        try:
            if not candidate.is_absolute():
                raise ValueError("workspace must be an absolute path")
            resolved = normalize_path(candidate, self._workspace)
            if not resolved.is_dir():
                raise ValueError("workspace must be an existing directory")
            return resolved
        except (SandboxPolicyError, OSError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, f"invalid workspace: {exc}") from exc

    # 检查真实运行锁和后台任务，清理已完成的注册表引用
    def is_busy(self, sid: str) -> bool:
        lock = self._locks.get(sid)
        if lock is not None and lock.locked():
            return True
        if sid in self._registries and self._registries[sid].is_busy():
            return True
        registries = [
            registry for registry in self._background_tasks.get(sid, [])
            if any(not task.done() for task, _ in registry.all())
        ]
        self._background_tasks[sid] = registries
        return bool(registries)

    # 列出当前项目和项目未知的聊天会话，优先使用仍在内存中的最新状态
    def list_sessions(self, *, workspace: Path | None = None) -> list[Session]:
        selected_workspace = self._workspace_for(workspace)
        sessions = {session.id: session for session in self._store.list_meta()}
        sessions.update(self._sessions)
        return sorted(
            (s for s in sessions.values()
             if s.mode == "chat" and self._matches_workspace(s, selected_workspace)),
            key=lambda s: (s.updated_at, s.id), reverse=True,
        )

    # 读取待恢复会话并将磁盘错误转换成稳定的协议错误
    def _load_for_resume(self, sid: str) -> Session:
        try:
            self._store.session_dir(sid)
            return self._sessions.get(sid) or self._store.read_meta(sid)
        except FileNotFoundError as exc:
            raise HandlerError(SESSION_NOT_FOUND, "session not found") from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HandlerError(SESSION_INVALID, "invalid or unreadable session") from exc

    # 恢复或切换聊天会话，提交状态和连接绑定后再发布事件
    async def resume(
        self,
        sid: str,
        current_session_id: str | None = None,
        *,
        on_commit: Callable[[], None] | None = None,
        workspace: Path | None = None,
    ) -> Session:
        self._check_accepting()
        selected_workspace = self._workspace_for(workspace)
        target = self._load_for_resume(sid)
        if target.mode != "chat":
            raise HandlerError(SESSION_NOT_RESUMABLE, "only chat sessions can be resumed")
        if not self._matches_workspace(target, selected_workspace):
            raise HandlerError(SESSION_PROJECT_MISMATCH, "session belongs to another project")
        current = self._get_session(current_session_id) if current_session_id else None
        if current is not None and not self._matches_workspace(current, selected_workspace):
            raise HandlerError(
                SESSION_PROJECT_MISMATCH, "current session belongs to another project",
            )
        ids = sorted({sid, *([current.id] if current is not None else [])})
        for candidate in ids:
            if self.is_busy(candidate):
                raise HandlerError(SESSION_BUSY, "session has a running task")
        async with AsyncExitStack() as stack:
            for candidate in ids:
                await stack.enter_async_context(self._locks.setdefault(candidate, asyncio.Lock()))
                self._flush_main(candidate)
            try:
                self._store.read_messages(sid)
                self._store.read_notes(sid)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise HandlerError(
                    SESSION_INVALID, "invalid or unreadable session history",
                ) from exc
            if current is target and target.status != "closed" and target.workspace is not None:
                if on_commit is not None:
                    on_commit()
                return target
            restored = replace(
                target, status="waiting_for_input", workspace=str(selected_workspace),
                updated_at=_now(),
            )
            closed = (
                replace(current, status="closed", updated_at=restored.updated_at)
                if current is not None and current.id != sid else None
            )
            try:
                self._store.write_meta(restored)
                if closed is not None:
                    try:
                        self._store.write_meta(closed)
                    except (OSError, ValueError):
                        self._store.write_meta(target)
                        raise
            except (OSError, ValueError) as exc:
                raise HandlerError(SESSION_INVALID, "could not persist restored session") from exc
            self._sessions[sid] = restored
            if closed is not None:
                self._sessions[closed.id] = closed
            if on_commit is not None:
                on_commit()
            if closed is not None:
                await self._release_agents(closed.id)
                await self._bus.publish(
                    SessionClosedEvent(session_id=closed.id, ts=closed.updated_at),
                )
            await self._bus.publish(SessionResumedEvent(session_id=sid, ts=restored.updated_at))
            return restored

    # 创建新 session 并写入 meta.json
    async def create(
        self, mode: SessionMode, title: str = "", *, workspace: Path | None = None,
    ) -> Session:
        self._check_accepting()
        selected_workspace = self._workspace_for(workspace)
        sid = f"sess-{uuid.uuid4().hex[:12]}"
        ts = _now()
        session = Session(
            id=sid,
            mode=mode,
            status="active",
            title=title,
            created_at=ts,
            updated_at=ts,
            run_ids=[],
            workspace=str(selected_workspace),
        )
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        self._store.write_meta(session)
        await self._bus.publish(SessionCreatedEvent(session_id=sid, mode=mode, ts=ts))
        return session

    # 处理用户消息，追加 thread 并启动一次 agent run
    async def send_message(self, sid: str, content: str, *, run_id: str | None = None) -> str:
        self._check_accepting()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")

            if session.status == "waiting_for_input":
                await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))

            run_id = run_id or new_run_id()
            session.status = "active"
            agents = self.agent_registry(sid)
            self._store.save_agent(agents.ensure_main().context, run_id)
            await agents.prepare_input(content)
            self._store.save_agent(agents.ensure_main().context, run_id)
            await self._bus.publish(
                SessionMessageReceivedEvent(
                    session_id=sid, content=content, run_id=run_id, ts=_now(),
                )
            )

            if not session.title:
                session.title = content[:40]

            session.run_ids.append(run_id)
            session.updated_at = _now()
            self._store.write_meta(session)

            # Skill 解析：检测 "/" 前缀，展开为系统提示覆盖和工具白名单
            goal = content
            system_prompt_override: str | None = None
            tool_whitelist: list[str] | None = None
            if content.startswith("/"):
                parts = content[1:].split(None, 1)
                skill_name = parts[0]
                arguments = parts[1] if len(parts) > 1 else ""
                loader = SkillLoader(
                    Path(session.workspace) if session.workspace else self._workspace,
                )
                skill = loader.resolve(skill_name)
                if skill is not None:
                    goal = loader.render_prompt(skill, arguments)
                    system_prompt_override = skill.system_prompt_template
                    tool_whitelist = skill.allowed_tools or None
                    await self._bus.publish(
                        SkillInvokedEvent(
                            skill_name=skill_name,
                            arguments=arguments,
                            run_id=run_id,
                            ts=_now(),
                        )
                    )

            runner = self._runner_factory()
            runner.bind_registry(agents)
            registry = getattr(runner, "background_tasks", None)
            if registry is not None:
                self._background_tasks[sid] = [registry]
            try:
                await runner.run_and_capture(
                    goal,
                    run_id=run_id,
                    session=session,
                    store=self._store,
                    system_prompt_override=system_prompt_override,
                    tool_whitelist=tool_whitelist,
                )
            finally:
                if session.mode == "one_shot":
                    await self._release_agents(sid)
                session.updated_at = _now()
                if session.mode == "one_shot":
                    session.status = "closed"
                    await self._bus.publish(
                        SessionClosedEvent(session_id=sid, ts=session.updated_at),
                    )
                else:
                    session.status = "waiting_for_input"
                    await self._bus.publish(
                        SessionWaitingForInputEvent(
                            session_id=sid, last_run_id=run_id, ts=session.updated_at,
                        )
                    )
                self._store.write_meta(session)
            return run_id

    # 关闭指定 session 并更新 meta.json
    async def close(self, sid: str) -> None:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if self.is_busy(sid):
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            self._flush_main(sid)
            await self._release_agents(sid)
            session.status = "closed"
            session.updated_at = _now()
            self._store.write_meta(session)
            await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))

    # 手动压缩指定 session 的 thread，将摘要持久化写入 thread.jsonl
    async def compact(self, sid: str, focus: str = "") -> Any:
        self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        if self._provider is None:
            raise HandlerError(-32020, "provider not available for compaction")
        async with lock:
            from kama_claude.core.bus.commands import SessionCompactResult
            from kama_claude.core.compact.compactor import Compactor
            from kama_claude.core.images import ImageStore
            from kama_claude.core.llm.images import ImageProvider
            agent = self.agent_registry(sid).ensure_main().context
            self._store.save_agent(agent, "compact")
            messages = agent.messages
            session_dir = self._store.session_dir(sid)
            compactor = Compactor(self._bus, session_dir, sid)
            provider = ImageProvider(self._provider, ImageStore(session_dir / "images"))
            result = await compactor.compact_messages(messages, provider, focus=focus)
            if result is None:
                raise HandlerError(-32021, "compaction failed or not beneficial")
            agent.messages = [
                {"role": "user", "content": result.summary_text},
                {"role": "assistant", "content": "Understood, I'll continue from this summary."},
            ]
            agent.history_replaced = True
            self._store.save_agent(agent, "compact")
            return SessionCompactResult(
                summary_tokens=result.summary_tokens,
                saved_tokens=max(0, result.original_token_estimate - result.summary_tokens),
            )

    # 读取指定 session 的完整 thread 历史
    async def get_history(self, sid: str) -> list[dict[str, Any]]:
        self._get_session(sid)
        return self._store.read_messages(sid)

    # 从内存索引取 session，不存在时抛 JSON-RPC 结构化错误
    def _get_session(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            raise HandlerError(SESSION_NOT_FOUND, "session not found")
        return session
