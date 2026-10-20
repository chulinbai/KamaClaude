from __future__ import annotations

import asyncio
import datetime
import fnmatch
import json
import logging
import signal
import time
from datetime import UTC
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import kama_claude
from kama_claude.core.bus.commands import (
    AgentRunCommand,
    AgentRunResult,
    EventSubscribeCommand,
    EventSubscribeResult,
    PermissionRespondCommand,
    PermissionRespondResult,
    PongResult,
    SessionCloseCommand,
    SessionCloseResult,
    SessionCompactCommand,
    SessionCompactResult,
    SessionCreateCommand,
    SessionCreateResult,
    SessionGetHistoryCommand,
    SessionGetHistoryResult,
    SessionListCommand,
    SessionListResult,
    SessionResumeCommand,
    SessionResumeResult,
    SessionSendMessageCommand,
    SessionSendMessageResult,
    SessionSummary,
    SubagentCloseCommand,
    SubagentDataResult,
    SubagentListCommand,
    SubagentListResult,
    SubagentResultCommand,
    SubagentSendInputCommand,
)
from kama_claude.core.bus.envelope import INVALID_PARAMS, EventPushEnvelope, HandlerError
from kama_claude.core.bus.events import SessionClosedEvent
from kama_claude.core.config import KamaConfig, get_config
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.provider import AnthropicProvider
from kama_claude.core.logging_setup import setup_logging
from kama_claude.core.mcp.server import McpServerManager
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.storage import load_policy_file
from kama_claude.core.runner import AgentRunner
from kama_claude.core.runs import events_file, new_run_id
from kama_claude.core.session import SessionManager, SessionStore
from kama_claude.core.session.manager import SESSION_BUSY, SESSION_IN_USE
from kama_claude.core.trace.record import TraceRecord
from kama_claude.core.trace.writer import TraceWriter
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.socket_server import SocketServer, get_connection_writer

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


class CoreApp:
    def __init__(self) -> None:
        self._start_time = time.monotonic()
        self._bus = EventBus()
        self._broadcaster: IpcEventBroadcaster | None = None
        self._trace: TraceWriter | None = None
        self._config: KamaConfig | None = None
        self._running_runs: set[asyncio.Task[Any]] = set()
        self._sessions: SessionManager | None = None
        self._permission_manager: PermissionManager | None = None
        self._mcp_manager: McpServerManager | None = None
        self._session_owners: dict[str, asyncio.StreamWriter] = {}
        self._session_transition_lock = asyncio.Lock()
        self._bus.subscribe(self._session_lifecycle)

    # 记录创建会话的连接，以便断连清理审批和会话路径权限
    def _own_session(self, session_id: str) -> None:
        try:
            writer = get_connection_writer()
        except LookupError:
            return  # 直接调用的单元测试或嵌入式使用没有 IPC 连接
        self._session_owners[session_id] = writer
        if writer.is_closing() and self._permission_manager is not None:
            self._permission_manager.cancel_session(session_id)

    # 校验会话由当前 IPC 连接持有，直接嵌入调用沿用无连接测试模式
    def _require_session_owner(self, session_id: str) -> None:
        try:
            writer = get_connection_writer()
        except LookupError:
            return
        if self._session_owners.get(session_id) is not writer:
            raise HandlerError(SESSION_IN_USE, "session is not attached to this connection")

    # 会话关闭事件统一清理一次性运行及显式关闭的授权
    async def _session_lifecycle(self, event: BaseModel) -> None:
        if isinstance(event, SessionClosedEvent):
            self._session_owners.pop(event.session_id, None)
            if self._permission_manager is not None:
                self._permission_manager.cancel_session(event.session_id, "session_closed")

    # 断连立即撤销授权；忙碌任务保持原生命周期但不能继续使用被撤销的权限
    async def _client_disconnected(self, writer: asyncio.StreamWriter) -> None:
        async with self._session_transition_lock:
            sessions = [sid for sid, owner in self._session_owners.items() if owner is writer]
            for sid in sessions:
                self._session_owners.pop(sid, None)
                if self._permission_manager is not None:
                    self._permission_manager.cancel_session(sid)
                if self._sessions is not None:
                    try:
                        await self._sessions.close(sid)
                    except HandlerError:
                        logger.info(
                            "Disconnected session still busy; authorizations revoked: %s", sid,
                        )

    # 处理 core.ping 请求，返回服务版本、运行时长和接收时间
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version=kama_claude.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )

    # 将 EventBus 事件写入 trace（作为 EventBus 订阅者）
    async def _trace_event_handler(self, event: BaseModel) -> None:
        assert self._trace is not None
        event_dict = event.model_dump()
        self._trace.emit(
            TraceRecord(
                ts=_now(),
                direction="CORE",
                layer="event",
                kind="event",
                run_id=event_dict.get("run_id"),
                data=event_dict,
            )
        )

    # 启动一次 agent run：异步创建 AgentRunner 并立即返回 run_id
    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        assert self._sessions is not None
        cmd = AgentRunCommand.model_validate(params)
        session = await self._sessions.create(
            mode="one_shot", title=cmd.goal[:40],
            workspace=Path(cmd.workspace) if cmd.workspace is not None else None,
        )
        self._own_session(session.id)
        run_id = new_run_id()
        run_task = asyncio.create_task(
            self._sessions.send_message(session.id, cmd.goal, run_id=run_id)
        )
        self._running_runs.add(run_task)
        run_task.add_done_callback(self._running_runs.discard)
        return AgentRunResult(run_id=run_id)

    # 创建 chat 或 one_shot session，并返回 session_id
    async def _session_create_handler(self, params: dict[str, Any]) -> SessionCreateResult:
        assert self._sessions is not None
        cmd = SessionCreateCommand.model_validate(params)
        session = await self._sessions.create(
            mode=cmd.mode, title=cmd.title,
            workspace=Path(cmd.workspace) if cmd.workspace is not None else None,
        )
        self._own_session(session.id)
        return SessionCreateResult(session_id=session.id, status=session.status)

    # 向 session 发送一条用户消息并同步等待对应 run 完成
    async def _session_send_handler(self, params: dict[str, Any]) -> SessionSendMessageResult:
        assert self._sessions is not None
        cmd = SessionSendMessageCommand.model_validate(params)
        async with self._session_transition_lock:
            self._require_session_owner(cmd.session_id)
        task = asyncio.current_task()
        if task is not None:
            self._running_runs.add(task)
        try:
            run_id = await self._sessions.send_message(cmd.session_id, cmd.content)
        finally:
            if task is not None:
                self._running_runs.discard(task)
        return SessionSendMessageResult(run_id=run_id)

    # 列出当前项目的聊天会话及相对于当前连接的占用状态
    async def _session_list_handler(self, params: dict[str, Any]) -> SessionListResult:
        assert self._sessions is not None
        cmd = SessionListCommand.model_validate(params)
        try:
            writer = get_connection_writer()
        except LookupError:
            writer = None
        return SessionListResult(sessions=[
            SessionSummary(
                session_id=s.id, title=s.title, updated_at=s.updated_at,
                workspace=s.workspace, status=s.status, busy=self._sessions.is_busy(s.id),
                attached_elsewhere=(
                    s.id in self._session_owners and self._session_owners[s.id] is not writer
                ),
            ) for s in self._sessions.list_sessions(
                workspace=Path(cmd.workspace) if cmd.workspace is not None else None,
            )
        ])

    # 校验占用后恢复会话，在状态提交时同步转移连接和重置临时权限
    async def _session_resume_handler(self, params: dict[str, Any]) -> SessionResumeResult:
        assert self._sessions is not None
        cmd = SessionResumeCommand.model_validate(params)
        async with self._session_transition_lock:
            try:
                writer = get_connection_writer()
            except LookupError:
                writer = None
            if writer is not None and writer.is_closing():
                raise HandlerError(SESSION_BUSY, "connection is closing")
            owner = self._session_owners.get(cmd.session_id)
            if owner is not None and owner is not writer:
                code = SESSION_BUSY if owner.is_closing() else SESSION_IN_USE
                raise HandlerError(code, "session is attached to another connection")
            if cmd.current_session_id:
                self._require_session_owner(cmd.current_session_id)
            already_owned = writer is not None and owner is writer

            # 在会话事件发出前提交连接归属，防止异步广播期间出现半恢复状态
            def attach() -> None:
                if cmd.current_session_id and cmd.current_session_id != cmd.session_id:
                    self._session_owners.pop(cmd.current_session_id, None)
                    if self._permission_manager is not None:
                        self._permission_manager.cancel_session(cmd.current_session_id)
                if not already_owned and self._permission_manager is not None:
                    self._permission_manager.reopen_session(cmd.session_id)
                self._own_session(cmd.session_id)

            session = await self._sessions.resume(
                cmd.session_id,
                cmd.current_session_id or (cmd.session_id if already_owned else None),
                on_commit=attach,
                workspace=Path(cmd.workspace) if cmd.workspace is not None else None,
            )
            return SessionResumeResult(
                session_id=session.id, title=session.title,
                workspace=session.workspace, status=session.status,
            )

    # 返回 session 的完整 Anthropic messages 历史
    async def _session_history_handler(self, params: dict[str, Any]) -> SessionGetHistoryResult:
        assert self._sessions is not None
        cmd = SessionGetHistoryCommand.model_validate(params)
        messages = await self._sessions.get_history(cmd.session_id)
        return SessionGetHistoryResult(messages=messages)

    # 列出当前连接所拥有会话中的全部 agent
    async def _subagent_list_handler(self, params: dict[str, Any]) -> SubagentListResult:
        assert self._sessions is not None
        cmd = SubagentListCommand.model_validate(params)
        async with self._session_transition_lock:
            self._require_session_owner(cmd.session_id)
            registry = self._sessions.agent_registry(cmd.session_id)
            return SubagentListResult(agents=registry.result()["agents"])

    # 查询指定身份或历史执行并返回明确的过期错误
    async def _subagent_result_handler(self, params: dict[str, Any]) -> SubagentDataResult:
        assert self._sessions is not None
        cmd = SubagentResultCommand.model_validate(params)
        async with self._session_transition_lock:
            self._require_session_owner(cmd.session_id)
            registry = self._sessions.agent_registry(cmd.session_id)
            try:
                return SubagentDataResult(data=registry.result(cmd.agent_id, cmd.run_id))
            except ValueError as exc:
                raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 用户通过管理界面投递协作消息，身份由服务端固定为 user
    async def _subagent_send_handler(self, params: dict[str, Any]) -> SubagentDataResult:
        assert self._sessions is not None
        cmd = SubagentSendInputCommand.model_validate(params)
        async with self._session_transition_lock:
            self._require_session_owner(cmd.session_id)
            registry = self._sessions.agent_registry(cmd.session_id)
        try:
            data = await registry.send("user", cmd.agent_id, cmd.message, cmd.wake)
            return SubagentDataResult(data=data)
        except ValueError as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 用户关闭子树时在状态校验后释放会话转换锁
    async def _subagent_close_handler(self, params: dict[str, Any]) -> SubagentDataResult:
        assert self._sessions is not None
        cmd = SubagentCloseCommand.model_validate(params)
        async with self._session_transition_lock:
            self._require_session_owner(cmd.session_id)
            registry = self._sessions.agent_registry(cmd.session_id)
        try:
            return SubagentDataResult(data=await registry.close("main", cmd.agent_id))
        except ValueError as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 接收客户端权限审批响应，resolve 对应挂起的 Future
    async def _permission_respond_handler(self, params: dict[str, Any]) -> PermissionRespondResult:
        cmd = PermissionRespondCommand.model_validate(params)
        logger.info(
            "permission.respond received tool_use_id=%s decision=%s",
            cmd.tool_use_id, cmd.decision,
        )
        if self._permission_manager is None:
            logger.error("permission.respond: PermissionManager not initialized")
            return PermissionRespondResult(ok=False)
        if cmd.request_id:
            sid = self._permission_manager.request_session(cmd.request_id)
            owner = self._session_owners.get(sid or "")
            if owner is not None and get_connection_writer() is not owner:
                return PermissionRespondResult(ok=False)
        return PermissionRespondResult(ok=self._permission_manager.respond(
            cmd.tool_use_id, cmd.decision, cmd.request_id, session_id=cmd.session_id,
        ))

    # 手动压缩 session thread，将摘要持久化写入 thread.jsonl
    async def _session_compact_handler(self, params: dict[str, Any]) -> SessionCompactResult:
        assert self._sessions is not None
        cmd = SessionCompactCommand.model_validate(params)
        async with self._session_transition_lock:
            self._require_session_owner(cmd.session_id)
        result = await self._sessions.compact(cmd.session_id, cmd.focus)
        return result  # type: ignore[no-any-return]

    # 关闭 session 并返回 closed 状态
    async def _session_close_handler(self, params: dict[str, Any]) -> SessionCloseResult:
        assert self._sessions is not None
        cmd = SessionCloseCommand.model_validate(params)
        async with self._session_transition_lock:
            self._require_session_owner(cmd.session_id)
            await self._sessions.close(cmd.session_id)
            if self._permission_manager is not None:
                self._permission_manager.cancel_session(cmd.session_id, "session_closed")
        return SessionCloseResult(status="closed")

    # 注册客户端事件订阅，可选先回放 events.jsonl 历史再接收实时流
    async def _subscribe_handler(self, params: dict[str, Any]) -> EventSubscribeResult:
        cmd = EventSubscribeCommand.model_validate(params)
        writer = get_connection_writer()

        replayed_count = 0
        if cmd.replay_from_run is not None:
            replayed_count = await self._replay_events(
                cmd.replay_from_run, writer, cmd.topics
            )

        assert self._broadcaster is not None
        sub_id = self._broadcaster.subscribe(writer, cmd.topics, cmd.scope)
        return EventSubscribeResult(subscription_id=sub_id, replayed_count=replayed_count)

    # 从 events.jsonl 向 writer 回放匹配 topic 的历史事件，返回已回放条数
    async def _replay_events(
        self,
        run_id: str,
        writer: asyncio.StreamWriter,
        topics: list[str],
    ) -> int:
        path = events_file(run_id)
        if not path.exists():
            for candidate in Path("~/.kama/sessions").expanduser().glob(
                f"*/runs/{run_id}/events.jsonl"
            ):
                path = candidate
                break
        if not path.exists():
            return 0

        count = 0
        for line in path.read_text().splitlines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type: str = event.get("type", "")
            if not any(fnmatch.fnmatch(event_type, p) for p in topics):
                continue
            envelope = EventPushEnvelope(event=event)
            writer.write(envelope.model_dump_json().encode() + b"\n")
            count += 1

        if count:
            await writer.drain()
        return count

    # 启动守护进程：加载配置、初始化日志、启动 trace、启动 TCP 服务器，并等待退出信号
    async def run(self) -> None:
        self._start_time = time.monotonic()
        self._config = get_config()
        setup_logging(self._config)

        if self._config.trace.enabled:
            trace_path = Path(self._config.trace.file).expanduser()
            self._trace = TraceWriter(trace_path)
            await self._trace.start()
            self._bus.subscribe(self._trace_event_handler)

        policy_file = Path("~/.kama/policy.toml").expanduser()
        self._permission_manager = PermissionManager(
            policy_file=policy_file,
            timeout_s=self._config.permission.timeout_s,
        )
        logger.info(
            "permission manager: timeout_s=%.1f  persistent=%d entries",
            self._config.permission.timeout_s,
            len(load_policy_file(policy_file)),
        )

        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        self._bus.subscribe(self._broadcaster.handle)
        sessions_root = Path("~/.kama/sessions").expanduser()
        store = SessionStore(sessions_root)
        assert self._config is not None
        compact_provider = AnthropicProvider(self._config.llm.default_model)

        self._mcp_manager = McpServerManager()
        if self._config.mcp.servers:
            logger.info("mcp: starting %d server(s)", len(self._config.mcp.servers))
            await self._mcp_manager.start_all(self._config.mcp.servers)

        self._sessions = SessionManager(
            store,
            runner_factory=lambda: AgentRunner(
                self._config,  # type: ignore[arg-type]
                bus=self._bus,
                trace=self._trace,
                permission_manager=self._permission_manager,
                mcp_manager=self._mcp_manager,
            ),
            bus=self._bus,
            provider=compact_provider,
        )

        server = SocketServer(
            self._config.host,
            self._config.port,
            self._broadcaster,
            trace=self._trace,
            on_disconnect=self._client_disconnected,
        )
        server.register("core.ping", self._ping_handler)
        server.register("agent.run", self._agent_run_handler)
        server.register("event.subscribe", self._subscribe_handler)
        server.register("session.create", self._session_create_handler)
        server.register("session.list", self._session_list_handler)
        server.register("session.resume", self._session_resume_handler)
        server.register("session.send_message", self._session_send_handler)
        server.register("session.get_history", self._session_history_handler)
        server.register("session.close", self._session_close_handler)
        server.register("permission.respond", self._permission_respond_handler)
        server.register("session.compact", self._session_compact_handler)
        server.register("subagent.list", self._subagent_list_handler)
        server.register("subagent.result", self._subagent_result_handler)
        server.register("subagent.send_input", self._subagent_send_handler)
        server.register("subagent.close", self._subagent_close_handler)

        addr = await server.start()
        logger.info("kama-core %s listening addr=%s", kama_claude.__version__, addr)
        logger.info("config: %s", self._config)

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        previous_handlers = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, shutdown.set)
            except NotImplementedError:
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda *_: loop.call_soon_threadsafe(shutdown.set))
        try:
            await shutdown.wait()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            await self._shutdown(server)

    # 先停止接收并收回所有主子执行，再关闭共享连接与服务
    async def _shutdown(self, server: SocketServer) -> None:
        logger.info("shutting down")
        if self._sessions is not None:
            self._sessions.stop_accepting()
        runs = list(self._running_runs)
        for run_task in runs:
            run_task.cancel()
        if runs:
            await asyncio.gather(*runs, return_exceptions=True)
        if self._sessions is not None:
            await self._sessions.shutdown()
        if self._mcp_manager is not None:
            await self._mcp_manager.stop_all()
        await server.stop()
        if self._trace is not None:
            await self._trace.stop()


# 同步入口：启动 CoreApp 事件循环
def run() -> None:
    asyncio.run(CoreApp().run())
