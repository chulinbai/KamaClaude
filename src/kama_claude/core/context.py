from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kama_claude.core.limits import normalize_max_steps

if TYPE_CHECKING:
    from kama_claude.core.agents.loader import AgentProfile
    from kama_claude.core.history.archive import HistoryArchive
    from kama_claude.core.history.models import HistoryRecord, RecordKind
    from kama_claude.core.subagent.registry import AgentRegistry


@dataclass
class _HistoryWrite:
    kind: RecordKind
    content: Any
    run_id: str | None
    source: dict[str, Any]
    record: HistoryRecord | None = None


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
    archive: HistoryArchive | None = field(default=None, repr=False)
    history_run_id: str | None = None
    pending_history: list[_HistoryWrite] = field(default_factory=list, repr=False)

    # 将主会话中新产生的消息归档，子会话没有归档接收器时保持原行为
    def record(
        self, kind: RecordKind, content: Any, *, source: dict[str, Any] | None = None,
    ) -> None:
        if self.archive is not None:
            self.pending_history.append(_HistoryWrite(
                kind, deepcopy(content), self.history_run_id, deepcopy(source or {}),
            ))
            self.flush_history()

    # 重试尚未提交的原文，保留首次分配的身份且绝不重新执行工具
    def flush_history(self) -> None:
        if self.archive is None:
            return
        while self.pending_history:
            pending = self.pending_history[0]

            # 在归档写锁内记住身份，部分写入失败后可安全检查重复提交
            def prepared(record: HistoryRecord) -> None:
                pending.record = record

            if pending.record is None:
                self.archive.emit(
                    pending.kind, {"content": pending.content}, pending.run_id,
                    source=pending.source, prepared=prepared,
                )
            else:
                self.archive.append_record(pending.record)
            self.pending_history.pop(0)

    # 保留已接收的原文并立即归档，写入失败时本轮停止且原文仍可重试
    def add_user_message(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})
        self.record("user_message", content)

    # 压缩前持久化原文与带覆盖边界的摘要，失败时不得替换当前消息
    def record_compaction(self, summary: str) -> None:
        if self.archive is not None:
            self.flush_history()
            checkpoint = self.archive.checkpoint()
            self.record(
                "compaction_summary", summary,
                source={"covers_through_sequence": checkpoint.sequence},
            )
            self.archive.checkpoint()


class ExecutionContext:
    # 创建单轮执行状态，兼容旧调用方并将历史统一交给 AgentContext
    def __init__(
        self,
        run_id: str,
        goal: str,
        max_steps: int | None = None,
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
        self.max_steps = normalize_max_steps(max_steps)
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
        self.agent_context.record("assistant_message", content)

    # 将工具调用结果追加为 user 消息；同一步的多个结果共享同一条消息
    def add_tool_result(
        self, tool_use_id: str, content: str | list[dict[str, Any]], is_error: bool = False,
    ) -> None:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True

        tool_name = ""
        for message in reversed(self.messages):
            blocks = message.get("content")
            if isinstance(blocks, list):
                for item in blocks:
                    if item.get("type") == "tool_use" and item.get("id") == tool_use_id:
                        tool_name = str(item.get("name", ""))
                        break
            if tool_name:
                break

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
        self.agent_context.record(
            "tool_result", [block], source={"tool_name": tool_name, "tool_use_id": tool_use_id},
        )

    # 返回 True 表示 loop 应停止（状态不再是 running）
    def is_done(self) -> bool:
        return self.status != "running"

    # 判断本轮还能否调用模型，无上限时始终允许下一步
    def has_steps_remaining(self) -> bool:
        return self.max_steps is None or self.step < self.max_steps

    # 将 run 标记为成功
    def mark_success(self) -> None:
        self.status = "success"

    # 将 run 标记为失败并记录原因
    def mark_failed(self, reason: str) -> None:
        self.status = "failed"
        self.reason = reason
