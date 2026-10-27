from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kama_claude.core.bus.events import (
    RunFinishedEvent,
    RunStartedEvent,
    SubagentFinishedEvent,
    SubagentStartedEvent,
)
from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.config import KamaConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus, EventHandler
from kama_claude.core.events.writer import EventWriter
from kama_claude.core.images import ImageStore
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.images import ImageProvider
from kama_claude.core.llm.provider import AnthropicProvider
from kama_claude.core.loop import AgentLoop
from kama_claude.core.mcp.server import McpServerManager
from kama_claude.core.memory.loader import load_context_file
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.paths import PathAccessController
from kama_claude.core.runs import RUNS_DIR, new_run_id
from kama_claude.core.sandbox.policy import configured_policy
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore
from kama_claude.core.subagent.registry import AgentRegistry
from kama_claude.core.subagent.tool import (
    AgentResultTool,
    CloseAgentTool,
    SendInputTool,
    SpawnAgentTool,
    WaitAgentTool,
)
from kama_claude.core.task.manager import TaskManager
from kama_claude.core.tools.builtin import (
    BashTool,
    CreateDirTool,
    ListDirTool,
    NoteSaveTool,
    ReadFileTool,
    ReadImageTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WriteFileTool,
)
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.trace.provider import TracingProvider
from kama_claude.core.trace.writer import TraceWriter


# 返回当前 UTC 时间
def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunOutcome:
    status: str
    result: str
    reason: str | None


class AgentRunner:
    # 组装共用执行服务，运行注册表由所属 Session 注入
    def __init__(
        self,
        config: KamaConfig,
        *,
        bus: EventBus | None = None,
        provider: LLMProvider | None = None,
        extra_handlers: list[EventHandler] | None = None,
        runs_dir: Path | None = None,
        trace: TraceWriter | None = None,
        permission_manager: PermissionManager | None = None,
        mcp_manager: McpServerManager | None = None,
    ) -> None:
        self._config = config
        self._bus = bus or EventBus()
        self._provider = provider
        self._extra_handlers = extra_handlers or []
        self._runs_dir = runs_dir or RUNS_DIR
        self._trace = trace
        self._permission_manager = permission_manager
        self._mcp_manager = mcp_manager
        self._task_registry = AgentRegistry(bus=self._bus)

    @property
    # 返回唯一运行表供旧调用方检查后台任务
    def background_tasks(self) -> AgentRegistry:
        return self._task_registry

    # 接入会话持有的注册表，保证下一轮仍能找到已有 agent
    def bind_registry(self, registry: AgentRegistry) -> None:
        self._task_registry = registry

    # 构建每轮工具实例，统一根与子 agent 的工具和 MCP 接入
    def _build_registry(
        self,
        task_manager: TaskManager,
        *,
        session: Session | None = None,
        store: SessionStore | None = None,
        run_id: str | None = None,
        provider: LLMProvider | None = None,
        bus: EventBus | None = None,
        child_runs_dir: Path | None = None,
        session_id: str = "",
        tool_whitelist: list[str] | None = None,
        context: ExecutionContext | None = None,
        workspace: Path | None = None,
        access: PathAccessController | None = None,
        images: ImageStore | None = None,
    ) -> ToolRegistry:
        allowed = set(tool_whitelist) if tool_whitelist else None

        # 角色白名单控制实际注册的工具，而不仅是模型可见的 schema
        def permitted(name: str) -> bool:
            return allowed is None or name in allowed

        workspace = workspace or (
            Path(session.workspace) if session and session.workspace else Path.cwd()
        )
        access = access or PathAccessController(
            cwd=workspace,
            policy=configured_policy(
                workspace,
                self._config.sandbox.filesystem,
                self._config.sandbox.windows,
            ),
        )
        registry = ToolRegistry()
        images = images or ImageStore(
            (store.session_dir(session.id) if store and session
             else self._runs_dir / (run_id or "standalone")) / "images"
        )
        tools = [
            ReadFileTool(access),
            ReadImageTool(images, access),
            BashTool(windows=self._config.sandbox.windows, access=access),
            WriteFileTool(access),
            ListDirTool(access),
            CreateDirTool(access),
            TaskCreateTool(task_manager),
            TaskUpdateTool(task_manager),
            TaskListTool(task_manager),
            TaskGetTool(task_manager),
        ]
        for tool in tools:
            if permitted(tool.name):
                registry.register(tool)
        if session is not None and store is not None and run_id is not None:
            if permitted("note_save"):
                registry.register(NoteSaveTool(store, session.id, run_id))
        if provider is not None and bus is not None and run_id is not None:
            runs_dir = child_runs_dir or self._runs_dir
            caller = context.agent_context.agent_id if context else "main"
            runtime = self._task_registry.agents.get(caller)

            # 子执行捕获工作目录与服务，但不捕获父运行的局部事件总线
            async def execute_child(child: ExecutionContext) -> None:
                await self.run_context(
                    child, workspace=workspace, runs_dir=runs_dir, access=access, images=images,
                )

            coordination = [
                SpawnAgentTool(
                    provider,
                    self._bus,
                    run_id,
                    self._permission_manager,
                    self._config.agent.max_steps,
                    self._task_registry,
                    runs_dir,
                    session_id,
                    depth=runtime.depth if runtime else 0,
                    filesystem_access=access,
                    caller=caller,
                    execute=execute_child,
                ),
                AgentResultTool(self._task_registry),
                SendInputTool(self._task_registry, caller),
                WaitAgentTool(self._task_registry, caller),
                CloseAgentTool(self._task_registry, caller),
            ]
            for tool in coordination:
                if permitted(tool.name):
                    registry.register(tool)
        if self._mcp_manager is not None:
            for tool in self._mcp_manager.get_tools():
                if permitted(tool.name):
                    registry.register(tool.with_images(images))
        return registry

    # 独立 one-shot 入口结束时也清理后台子任务
    async def run(self, goal: str, *, run_id: str | None = None) -> None:
        try:
            await self.run_and_capture(goal, run_id=run_id)
        finally:
            await self._task_registry.shutdown()

    # 普通会话适配层，保持 Session 存储与单轮配置接口
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
    ) -> RunOutcome:
        run_id = run_id or new_run_id()
        sid = session.id if session else ""
        self._task_registry.session_id = sid
        if "main" not in self._task_registry.agents:
            history = (
                store.read_messages(sid)
                if store and session
                else [{"role": "user", "content": goal}]
            )
            runtime = self._task_registry.ensure_main(history)
        else:
            runtime = self._task_registry.agents["main"]
            if session is None:
                runtime.context.messages.append({"role": "user", "content": goal})
        context = ExecutionContext(
            run_id,
            goal,
            self._config.agent.max_steps,
            agent_context=runtime.context,
            registry=self._task_registry,
            system_prompt_override=system_prompt_override,
            session_notes=store.read_notes(sid) if store and session else "",
        )
        try:
            await self._task_registry.begin_main(context)
            await self.run_context(
                context,
                workspace=Path(session.workspace) if session and session.workspace else Path.cwd(),
                runs_dir=store.runs_dir(sid) if store and session else self._runs_dir,
                session=session,
                store=store,
                tool_whitelist=tool_whitelist,
            )
        finally:
            record = self._task_registry.runs.get(context.run_id)
            if record is not None and record.context is context:
                if context.status == "running":
                    context.mark_failed("cancelled")
                await self._task_registry.finish(context)
        return RunOutcome(context.status, context.result, context.reason)

    # 共用执行内核，仅通过可选能力接入主会话的笔记和持久化
    async def run_context(
        self,
        context: ExecutionContext,
        *,
        workspace: Path,
        runs_dir: Path,
        session: Session | None = None,
        store: SessionStore | None = None,
        tool_whitelist: list[str] | None = None,
        access: PathAccessController | None = None,
        parent_run_id: str | None = None,
        images: ImageStore | None = None,
    ) -> None:
        agent = context.agent_context
        run_path = runs_dir / context.run_id
        images = images or ImageStore(
            (store.session_dir(session.id) if store and session else run_path) / "images"
        )
        runtime = self._task_registry.agents[agent.agent_id]
        bus = EventBus(
            session_id=agent.session_id,
            agent_id=agent.agent_id,
            parent_agent_id=agent.parent_agent_id,
        )
        context.global_context = load_context_file(Path("~/.kama/context.md").expanduser())
        context.project_context = load_context_file(workspace / ".kama/context.md")
        if agent.profile is not None:
            tool_whitelist = agent.profile.allowed_tools or None
        if agent.parent_agent_id:
            parent = self._task_registry.agents[agent.parent_agent_id]
            parent_run_id = parent_run_id or parent.latest_run_id or ""
        cancelled = False
        persistence_error: Exception | None = None
        async with EventWriter(run_path / "events.jsonl") as writer:
            writer.subscribe(bus)
            bus.subscribe(self._bus.publish)
            for handler in self._extra_handlers:
                bus.subscribe(handler)
            await bus.publish(RunStartedEvent(run_id=context.run_id, goal=context.goal, ts=_now()))
            try:
                if agent.parent_agent_id:
                    await bus.publish(
                        SubagentStartedEvent(
                            run_id=context.run_id,
                            parent_run_id=parent_run_id or "",
                            description=agent.description,
                            ts=_now(),
                        )
                    )
                if self._provider is None:
                    self._provider = AnthropicProvider(self._config.llm.default_model)
                provider: LLMProvider = ImageProvider(self._provider, images)
                if self._trace is not None:
                    provider = TracingProvider(
                        provider,
                        self._trace,
                        include_payload=self._config.trace.include_llm_payload,
                    )
                task_manager = TaskManager(runtime.tasks_dir or run_path / ".tasks")
                tools = self._build_registry(
                    task_manager,
                    session=session,
                    store=store,
                    run_id=context.run_id,
                    provider=provider,
                    bus=bus,
                    child_runs_dir=runs_dir,
                    session_id=agent.session_id,
                    tool_whitelist=tool_whitelist,
                    context=context,
                    workspace=workspace,
                    access=access,
                    images=images,
                )
                compactor = Compactor(
                    bus,
                    store.session_dir(session.id) if store and session else run_path,
                    agent.session_id,
                )
                loop = AgentLoop(
                    provider,
                    tools,
                    bus,
                    permission_manager=self._permission_manager,
                    compactor=compactor,
                    compact_threshold=self._config.compaction.auto_threshold,
                    session_id=agent.session_id,
                )
                await loop.run(context)
            except asyncio.CancelledError:
                cancelled = True
                context.mark_failed("cancelled")
            except Exception:
                logging.getLogger(__name__).exception("run failed run_id=%s", context.run_id)
                context.mark_failed("execution_error")
            finally:
                runtime.accepting = False
                context.repair_tool_results()
                if session is not None and store is not None:
                    try:
                        store.save_agent(agent, context.run_id)
                    except Exception as exc:
                        context.mark_failed("persistence_error")
                        persistence_error = exc
                if agent.parent_agent_id:
                    await bus.publish(
                        SubagentFinishedEvent(
                            run_id=context.run_id,
                            parent_run_id=parent_run_id or "",
                            status=context.status,
                            ts=_now(),
                        )
                    )
                await bus.publish(
                    RunFinishedEvent(
                        run_id=context.run_id,
                        status=context.status,
                        reason=context.reason,
                        steps=context.step,
                        ts=_now(),
                    )
                )
        if persistence_error is not None:
            raise persistence_error
        if cancelled:
            raise asyncio.CancelledError()
