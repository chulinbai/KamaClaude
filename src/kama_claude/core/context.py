from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kama_claude.core.agents.loader import AgentProfile
    from kama_claude.core.subagent.registry import AgentRegistry


@dataclass
class AgentContext:
    agent_id: str = "main"
    session_id: str = ""
    parent_agent_id: str | None = None
    description: str = ""
    profile: AgentProfile | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    persisted_count: int = 0
    history_replaced: bool = False


class ExecutionContext:
    # 创建单轮执行状态，兼容旧调用方并将历史统一交给 AgentContext
    def __init__(
        self,
        run_id: str,
        goal: str,
        max_steps: int,
        prefill_messages: list[dict[str, Any]] | None = None,
        session_notes: str = "",
        global_context: str = "",
        project_context: str = "",
        messages: list[dict[str, Any]] | None = None,
        step: int = 0,
        status: str = "running",
        reason: str | None = None,
        result: str = "",
        system_prompt_override: str | None = None,
        *,
        agent_context: AgentContext | None = None,
        registry: AgentRegistry | None = None,
    ) -> None:
        self.agent_context = (
            agent_context
            if agent_context is not None
            else AgentContext(
                messages=deepcopy(
                    prefill_messages or messages or [{"role": "user", "content": goal}]
                ),
            )
        )
        self.run_id = run_id
        self.goal = goal
        self.max_steps = max_steps
        self.session_notes = session_notes
        self.global_context = global_context
        self.project_context = project_context
        self.step = step
        self.status = status
        self.reason = reason
        self.result = result
        self.system_prompt_override = system_prompt_override
        self.registry = registry

    @property
    # 读取所属 agent 唯一的可变历史
    def messages(self) -> list[dict[str, Any]]:
        return self.agent_context.messages

    @messages.setter
    # 压缩替换历史时标记完整持久化，避免沿用旧列表的增量下标
    def messages(self, value: list[dict[str, Any]]) -> None:
        self.agent_context.messages = value
        self.agent_context.history_replaced = True

    # 修复中断批次，保留已完成结果并明确未知副作用
    def repair_tool_results(self) -> None:
        pending: dict[str, None] = {}
        for message in self.messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_use":
                    pending[str(block["id"])] = None
                elif block.get("type") == "tool_result":
                    pending.pop(str(block["tool_use_id"]), None)
        for tool_id in pending:
            self.add_tool_result(
                tool_id,
                "Execution interrupted; completion and side effects are unknown.",
                is_error=True,
            )

    # 返回当前 run 的 system prompt；有 override 时跳过 base，直接注入记忆层
    def system_prompt(self, base: str) -> str:
        parts = [self.system_prompt_override if self.system_prompt_override else base]
        if self.global_context.strip():
            parts.append("\n\n## Global Context\n" + self.global_context.strip())
        if self.project_context.strip():
            parts.append("\n\n## Project Context\n" + self.project_context.strip())
        if self.session_notes.strip():
            parts.append(
                "\n\n## Session Notes\n"
                + self.session_notes.strip()
                + "\n\nRemember important durable facts by calling note_save."
            )
        return "".join(parts)

    # 将 LLM 响应的 content blocks 追加为 assistant 消息
    def add_assistant_message(self, content: list[Any]) -> None:
        self.messages.append({"role": "assistant", "content": content})

    # 将工具调用结果追加为 user 消息；同一步的多个结果共享同一条消息
    def add_tool_result(self, tool_use_id: str, content: str, is_error: bool = False) -> None:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True

        last = self.messages[-1] if self.messages else None
        if (
            last is not None
            and last["role"] == "user"
            and isinstance(last["content"], list)
            and last["content"]
            and all(b.get("type") == "tool_result" for b in last["content"])
        ):
            last["content"].append(block)
        else:
            self.messages.append({"role": "user", "content": [block]})

    # 返回 True 表示 loop 应停止（状态不再是 running）
    def is_done(self) -> bool:
        return self.status != "running"

    # 将 run 标记为成功
    def mark_success(self) -> None:
        self.status = "success"

    # 将 run 标记为失败并记录原因
    def mark_failed(self, reason: str) -> None:
        self.status = "failed"
        self.reason = reason
