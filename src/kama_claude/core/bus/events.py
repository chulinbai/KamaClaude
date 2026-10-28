from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field

from kama_claude.core.permissions.paths import PathGrant


class AgentEvent(BaseModel):
    session_id: str | None = None
    agent_id: str | None = None
    parent_agent_id: str | None = None


class CoreStartedEvent(BaseModel):
    type: Literal["core.started"] = "core.started"
    listen_addr: str  # e.g. "127.0.0.1:7437"
    version: str


class RunStartedEvent(AgentEvent):
    type: Literal["run.started"] = "run.started"
    run_id: str
    goal: str
    ts: str  # ISO 8601


class RunFinishedEvent(AgentEvent):
    type: Literal["run.finished"] = "run.finished"
    run_id: str
    status: str  # "success" | "failed"
    reason: str | None = None  # "exceeded_max_steps" | "cancelled" | "llm_error" | ...
    steps: int
    ts: str


class StepStartedEvent(AgentEvent):
    type: Literal["step.started"] = "step.started"
    run_id: str
    step: int
    ts: str


class StepFinishedEvent(AgentEvent):
    type: Literal["step.finished"] = "step.finished"
    run_id: str
    step: int
    ts: str


class ToolCallStartedEvent(AgentEvent):
    type: Literal["tool.call_started"] = "tool.call_started"
    run_id: str
    tool_use_id: str
    tool_name: str
    params: dict[str, Any]
    ts: str


class ToolCallFinishedEvent(AgentEvent):
    type: Literal["tool.call_finished"] = "tool.call_finished"
    run_id: str
    tool_use_id: str
    tool_name: str
    elapsed_ms: int
    output: str = ""  # tool result content, for TUI display
    ts: str


class ToolCallFailedEvent(AgentEvent):
    type: Literal["tool.call_failed"] = "tool.call_failed"
    run_id: str
    tool_use_id: str
    tool_name: str
    # "runtime_error" | "timeout" | "schema_error" | "permission_denied" | "rate_limited"
    error_class: str
    error_message: str
    elapsed_ms: int
    attempt: int = 1  # 1=first attempt, 2=first retry, 3=second retry
    ts: str


class LlmTokenEvent(AgentEvent):
    type: Literal["llm.token"] = "llm.token"
    run_id: str
    token: str
    ts: str


class LlmUsageEvent(AgentEvent):
    type: Literal["llm.usage"] = "llm.usage"
    run_id: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    context_pct: float = 0.0
    ts: str


class LlmModelSelectedEvent(AgentEvent):
    type: Literal["llm.model_selected"] = "llm.model_selected"
    run_id: str
    model: str
    strategy: str  # "static" | "rule_based" | "cost_budget"
    ts: str


class LogLineEvent(AgentEvent):
    type: Literal["log.line"] = "log.line"
    run_id: str
    level: str  # "DEBUG" | "INFO" | "WARNING" | "ERROR"
    source: str
    message: str
    ts: str


class SessionCreatedEvent(BaseModel):
    type: Literal["session.created"] = "session.created"
    session_id: str
    mode: str
    ts: str


class SessionMessageReceivedEvent(BaseModel):
    type: Literal["session.message_received"] = "session.message_received"
    session_id: str
    content: str
    run_id: str | None = None
    ts: str


class SessionWaitingForInputEvent(BaseModel):
    type: Literal["session.waiting_for_input"] = "session.waiting_for_input"
    session_id: str
    last_run_id: str
    ts: str


class SessionResumedEvent(BaseModel):
    type: Literal["session.resumed"] = "session.resumed"
    session_id: str
    ts: str


class SessionClosedEvent(BaseModel):
    type: Literal["session.closed"] = "session.closed"
    session_id: str
    ts: str


class ContextCompactedEvent(AgentEvent):
    type: Literal["context.compacted"] = "context.compacted"
    session_id: str
    run_id: str
    original_tokens: int
    summary_tokens: int
    ts: str


class PermissionRequestedEvent(AgentEvent):
    type: Literal["permission.requested"] = "permission.requested"
    run_id: str
    tool_use_id: str
    tool_name: str
    params: dict[str, Any]
    param_preview: str
    session_id: str
    request_id: str | None = None
    workspace: str = ""
    reason: str = "tool_policy"
    paths: list[PathGrant] = Field(default_factory=list)
    history_workspaces: list[str | None] = Field(default_factory=list)
    allowed_decisions: list[str] = Field(default_factory=lambda: [
        "allow_once", "always_allow", "deny_once", "always_deny",
    ])
    ts: str


class PermissionGrantedEvent(AgentEvent):
    type: Literal["permission.granted"] = "permission.granted"
    run_id: str
    tool_use_id: str
    request_id: str | None = None
    # "allow_once" | "always_allow" | "auto_allow"
    decision: str
    ts: str


class PermissionDeniedEvent(AgentEvent):
    type: Literal["permission.denied"] = "permission.denied"
    run_id: str
    tool_use_id: str
    request_id: str | None = None
    # "deny_once" | "always_deny" | "auto_deny"
    decision: str
    ts: str


class SubagentStartedEvent(AgentEvent):
    type: Literal["subagent.started"] = "subagent.started"
    run_id: str          # 子 agent run_id
    parent_run_id: str
    description: str
    ts: str


class SubagentFinishedEvent(AgentEvent):
    type: Literal["subagent.finished"] = "subagent.finished"
    run_id: str
    parent_run_id: str
    status: str          # "success" | "failed"
    ts: str


class SkillInvokedEvent(AgentEvent):
    type: Literal["skill.invoked"] = "skill.invoked"
    skill_name: str
    arguments: str
    run_id: str
    ts: str


class AgentStateEvent(AgentEvent):
    type: Literal["subagent.state_changed"] = "subagent.state_changed"
    run_id: str | None = None
    snapshot: dict[str, Any]
    ts: str


class AgentMessageEvent(AgentEvent):
    type: Literal["subagent.message"] = "subagent.message"
    run_id: str | None = None
    message_id: str
    sender_agent_id: str
    status: str
    ts: str


# 根据 type 字段决定事件类型的判别联合
Event = Annotated[
    CoreStartedEvent
    | RunStartedEvent
    | RunFinishedEvent
    | StepStartedEvent
    | StepFinishedEvent
    | ToolCallStartedEvent
    | ToolCallFinishedEvent
    | ToolCallFailedEvent
    | LlmTokenEvent
    | LlmUsageEvent
    | LlmModelSelectedEvent
    | LogLineEvent
    | SessionCreatedEvent
    | SessionMessageReceivedEvent
    | SessionWaitingForInputEvent
    | SessionResumedEvent
    | SessionClosedEvent
    | ContextCompactedEvent
    | PermissionRequestedEvent
    | PermissionGrantedEvent
    | PermissionDeniedEvent
    | SubagentStartedEvent
    | SubagentFinishedEvent
    | SkillInvokedEvent
    | AgentStateEvent
    | AgentMessageEvent,
    Discriminator("type"),
]
