from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.agents.loader import AgentProfileLoader
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.permissions.paths import PathAccessController
from kama_claude.core.sandbox.models import WindowsSandboxConfig
from kama_claude.core.subagent.registry import AgentRegistry, Executor
from kama_claude.core.tools.base import BaseTool, ToolResult

if TYPE_CHECKING:
    from kama_claude.core.llm.base import LLMProvider
    from kama_claude.core.permissions.manager import PermissionManager


class SpawnAgentParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    description: str
    prompt: str
    run_in_background: bool = False
    subagent_type: str = ""


class CoordinationTool(BaseTool):
    retry_errors = False
    managed_timeout = True

    # 将协调操作结果编码为可供模型读取的 JSON
    @staticmethod
    def output(value: dict[str, Any]) -> ToolResult:
        return ToolResult(
            content=json.dumps(value, ensure_ascii=False), is_error=value.get("status") == "failed"
        )

    # 将身份或状态错误转换为不会自动重试的工具错误
    @staticmethod
    def error(exc: ValueError) -> ToolResult:
        return ToolResult(content=str(exc), is_error=True, error_type="coordination_error")


class SpawnAgentTool(CoordinationTool):
    name = "spawn_agent"
    description = (
        "Create an agent with independent history from the supplied prompt. "
        "Returns stable agent_id and run_id. Background runs return immediately. "
        "Use send_input to continue the same agent, agent_result to inspect it, "
        "and close_agent to release an idle agent's capacity."
    )
    params_model = SpawnAgentParams
    input_schema = SpawnAgentParams.model_json_schema()

    # 保留原构造接口，同时允许 Runner 注入统一执行内核
    def __init__(
        self,
        provider: LLMProvider,
        parent_bus: EventBus,
        parent_run_id: str,
        permission_manager: PermissionManager | None,
        max_steps: int,
        task_registry: AgentRegistry,
        runs_dir: Path,
        session_id: str,
        depth: int = 0,
        windows_sandbox: WindowsSandboxConfig | None = None,
        filesystem_access: PathAccessController | None = None,
        *,
        caller: str = "main",
        execute: Executor | None = None,
    ) -> None:
        self._registry = task_registry
        self._caller = caller
        self._runs_dir = runs_dir
        self._max_steps = max_steps
        self._depth = depth
        self._workspace = filesystem_access.cwd if filesystem_access else Path.cwd()
        if execute is None:
            from kama_claude.core.config import KamaConfig
            from kama_claude.core.runner import AgentRunner

            config = KamaConfig()
            config.agent.max_steps = max_steps
            if windows_sandbox is not None:
                config.sandbox.windows = windows_sandbox
            runner = AgentRunner(
                config,
                provider=provider,
                bus=parent_bus,
                permission_manager=permission_manager,
                runs_dir=runs_dir,
            )
            self._registry.session_id = session_id
            self._registry.bus = parent_bus
            self._registry.ensure_main()
            runner.bind_registry(task_registry)

            # 旧入口也委托共用 Runner，不再构造另一套子执行循环
            async def execute_child(context: ExecutionContext) -> None:
                await runner.run_context(
                    context,
                    workspace=self._workspace,
                    runs_dir=runs_dir,
                    access=filesystem_access,
                    parent_run_id=parent_run_id,
                )

            execute = execute_child
        self._execute = execute

    # 创建稳定身份并选择等待初始 Run 或立即返回
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = SpawnAgentParams.model_validate(params)
        try:
            if self._depth >= 2:
                raise ValueError("Subagent nesting limit (2) reached")
            profile = (
                AgentProfileLoader(self._workspace).load(p.subagent_type)
                if p.subagent_type
                else None
            )
            if p.subagent_type and profile is None:
                raise ValueError("Unknown or invalid subagent role")
            agent_id, run_id = await self._registry.spawn(
                self._caller,
                p.description,
                p.prompt,
                profile,
                self._execute,
                self._max_steps,
                self._runs_dir,
            )
            if p.run_in_background:
                return ToolResult(
                    content=f"agent_id={agent_id}. run_id={run_id}. Use agent_result to query."
                )
            return self.output(await self._registry.join(run_id))
        except ValueError as exc:
            return self.error(exc)


class AgentResultParams(BaseModel):
    agent_id: str | None = None
    run_id: str | None = None


class AgentResultTool(CoordinationTool):
    name = "agent_result"
    description = "List agents with no arguments; inspect an agent_id or a specific run_id."
    params_model = AgentResultParams
    input_schema = AgentResultParams.model_json_schema()

    # 注入当前会话的唯一运行注册表
    def __init__(self, task_registry: AgentRegistry) -> None:
        self._registry = task_registry

    # 返回指定执行的结果，不把旧 Run 查询替换为最近结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = AgentResultParams.model_validate(params)
        try:
            result = self._registry.result(p.agent_id, p.run_id)
            if p.run_id and result.get("status") == "running":
                return ToolResult(content="still running")
            return self.output(result)
        except ValueError as exc:
            return self.error(exc)


class SendInputParams(BaseModel):
    agent_id: str
    message: str
    wake: bool = False


class SendInputTool(CoordinationTool):
    name = "send_input"
    description = (
        "Send a message to another agent in this session. Running agents consume it at a safe "
        "checkpoint; wake=true also starts an idle child. Main cannot be automatically woken. "
        "Acceptance does not mean the message has been processed."
    )
    params_model = SendInputParams
    input_schema = SendInputParams.model_json_schema()

    # 绑定运行时提供的发送者身份
    def __init__(self, registry: AgentRegistry, caller: str) -> None:
        self._registry = registry
        self._caller = caller

    # 投递消息时不等待接收方完成任务
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = SendInputParams.model_validate(params)
        try:
            return self.output(
                await self._registry.send(self._caller, p.agent_id, p.message, p.wake)
            )
        except ValueError as exc:
            return self.error(exc)


class WaitAgentParams(BaseModel):
    agent_ids: list[str] = Field(default_factory=list)
    timeout_ms: int = Field(default=30000, ge=0, le=60000)


class WaitAgentTool(CoordinationTool):
    name = "wait_agent"
    description = (
        "Wait for any selected descendant's current run or an incoming message. "
        "Without agent_ids wait only for messages. Does not consume messages or cancel targets."
    )
    params_model = WaitAgentParams
    input_schema = WaitAgentParams.model_json_schema()

    # 绑定调用者身份以检查后代范围和对应收件箱
    def __init__(self, registry: AgentRegistry, caller: str) -> None:
        self._registry = registry
        self._caller = caller

    # 等待固定执行或消息就绪，超时保持目标运行
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = WaitAgentParams.model_validate(params)
        try:
            return self.output(await self._registry.wait(self._caller, p.agent_ids, p.timeout_ms))
        except ValueError as exc:
            return self.error(exc)


class CloseAgentParams(BaseModel):
    agent_id: str


class CloseAgentTool(CoordinationTool):
    name = "close_agent"
    description = (
        "Close a descendant and its entire subtree, cancelling runs and releasing capacity."
    )
    params_model = CloseAgentParams
    input_schema = CloseAgentParams.model_json_schema()

    # 注入调用者以限制关闭范围
    def __init__(self, registry: AgentRegistry, caller: str) -> None:
        self._registry = registry
        self._caller = caller

    # 等待子树清理完成，重复关闭已有终态记录保持幂等
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = CloseAgentParams.model_validate(params)
        try:
            return self.output(await self._registry.close(self._caller, p.agent_id))
        except ValueError as exc:
            return self.error(exc)
