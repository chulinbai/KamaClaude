from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field

from kama_claude.core.history.models import HistoryReadParams, HistorySearchParams
from kama_claude.core.session.model import SessionMode, SessionStatus


class PingCommand(BaseModel):
    type: Literal["core.ping"] = "core.ping"
    client: str


class PongResult(BaseModel):
    server_version: str
    uptime_ms: int
    received_at: str  # ISO 8601


class AgentRunCommand(BaseModel):
    type: Literal["agent.run"] = "agent.run"
    goal: str
    workspace: str | None = Field(default=None, min_length=1)
    max_steps: int | None = Field(
        default=None, ge=0, strict=True,
        description="Per-run step limit: omitted/null uses Core config; 0 is unlimited.",
    )


class AgentRunResult(BaseModel):
    run_id: str


class EventSubscribeCommand(BaseModel):
    type: Literal["event.subscribe"] = "event.subscribe"
    topics: list[str]          # fnmatch 模式，如 ["step.*", "tool.*"]
    scope: str = "global"      # "global" | "run:<run_id>"
    replay_from_run: str | None = None  # 设置则先从 events.jsonl 回放历史再接实时流


class EventSubscribeResult(BaseModel):
    subscription_id: str
    replayed_count: int = 0


class SessionCreateCommand(BaseModel):
    type: Literal["session.create"] = "session.create"
    mode: SessionMode = "chat"
    title: str = ""
    workspace: str | None = Field(default=None, min_length=1)


class SessionCreateResult(BaseModel):
    session_id: str
    status: SessionStatus


class SessionListCommand(BaseModel):
    type: Literal["session.list"] = "session.list"
    workspace: str | None = Field(default=None, min_length=1)


class SessionSummary(BaseModel):
    session_id: str
    title: str
    updated_at: str
    workspace: str | None
    status: SessionStatus
    busy: bool = False
    attached_elsewhere: bool = False


class SessionListResult(BaseModel):
    sessions: list[SessionSummary]


class SessionResumeCommand(BaseModel):
    type: Literal["session.resume"] = "session.resume"
    session_id: str
    current_session_id: str | None = None
    workspace: str | None = Field(default=None, min_length=1)


class SessionResumeResult(BaseModel):
    session_id: str
    title: str
    workspace: str | None
    status: SessionStatus


class SessionSendMessageCommand(BaseModel):
    type: Literal["session.send_message"] = "session.send_message"
    session_id: str
    content: str
    max_steps: int | None = Field(
        default=None, ge=0, strict=True,
        description="Per-message run step limit: omitted/null uses Core config; 0 is unlimited.",
    )


class SessionSendMessageResult(BaseModel):
    run_id: str


class SessionGetHistoryCommand(BaseModel):
    type: Literal["session.get_history"] = "session.get_history"
    session_id: str


class SessionGetHistoryResult(BaseModel):
    messages: list[dict[str, Any]]


class SessionCloseCommand(BaseModel):
    type: Literal["session.close"] = "session.close"
    session_id: str


class SessionCloseResult(BaseModel):
    status: SessionStatus


class PermissionRespondCommand(BaseModel):
    type: Literal["permission.respond"] = "permission.respond"
    tool_use_id: str
    request_id: str | None = None
    session_id: str | None = None
    # 路径审批使用 allow_once / allow_session / deny_once，禁止持久化工具授权
    decision: str


class PermissionRespondResult(BaseModel):
    ok: bool = True


class SessionCompactCommand(BaseModel):
    type: Literal["session.compact"] = "session.compact"
    session_id: str
    focus: str = ""


class SessionCompactResult(BaseModel):
    summary_tokens: int
    saved_tokens: int


class SubagentListCommand(BaseModel):
    type: Literal["subagent.list"] = "subagent.list"
    session_id: str


class SubagentResultCommand(BaseModel):
    type: Literal["subagent.result"] = "subagent.result"
    session_id: str
    agent_id: str | None = None
    run_id: str | None = None


class SubagentSendInputCommand(BaseModel):
    type: Literal["subagent.send_input"] = "subagent.send_input"
    session_id: str
    agent_id: str
    message: str = Field(min_length=1, max_length=16384)
    wake: bool = False


class SubagentCloseCommand(BaseModel):
    type: Literal["subagent.close"] = "subagent.close"
    session_id: str
    agent_id: str


class SubagentListResult(BaseModel):
    agents: list[dict[str, Any]]


class SubagentDataResult(BaseModel):
    data: dict[str, Any]


class HistorySearchCommand(HistorySearchParams):
    type: Literal["history.search"] = "history.search"
    workspace: str | None = Field(default=None, min_length=1)


class HistoryReadCommand(HistoryReadParams):
    type: Literal["history.read"] = "history.read"
    workspace: str | None = Field(default=None, min_length=1)


class HistoryStatusCommand(BaseModel):
    type: Literal["history.status"] = "history.status"
    workspace: str | None = Field(default=None, min_length=1)
    scope: Literal["workspace", "all"] = "workspace"


class HistoryReindexCommand(BaseModel):
    type: Literal["history.reindex"] = "history.reindex"
    workspace: str | None = Field(default=None, min_length=1)
    scope: Literal["workspace", "all"] = "workspace"


# 根据 type 字段决定命令类型的判别联合
Command = Annotated[
    PingCommand
    | AgentRunCommand
    | EventSubscribeCommand
    | SessionCreateCommand
    | SessionListCommand
    | SessionResumeCommand
    | SessionSendMessageCommand
    | SessionGetHistoryCommand
    | SessionCloseCommand
    | PermissionRespondCommand
    | SessionCompactCommand
    | SubagentListCommand
    | SubagentResultCommand
    | SubagentSendInputCommand
    | SubagentCloseCommand
    | HistorySearchCommand
    | HistoryReadCommand
    | HistoryStatusCommand
    | HistoryReindexCommand,
    Discriminator("type"),
]
