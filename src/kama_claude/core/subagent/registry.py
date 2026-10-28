from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from kama_claude.core.agents.loader import AgentProfile
from kama_claude.core.bus.events import AgentMessageEvent, AgentStateEvent
from kama_claude.core.context import AgentContext, ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.limits import normalize_max_steps
from kama_claude.core.runs import new_run_id

Executor = Callable[[ExecutionContext], Awaitable[None]]


@dataclass
class AgentMessage:
    message_id: str
    sender: str
    content: str
    wake: bool = False


@dataclass
class AgentRuntime:
    context: AgentContext
    depth: int = 0
    state: str = "IDLE"
    inbox: deque[AgentMessage] = field(default_factory=deque)
    current: ExecutionContext | None = None
    task: asyncio.Task[Any] | None = None
    execute: Executor | None = None
    max_steps: int | None = None
    accepting: bool = False
    latest_run_id: str | None = None
    tasks_dir: Path | None = None
    close_task: asyncio.Task[None] | None = None
    discarded_messages: int = 0


@dataclass
class RunRecord:
    context: ExecutionContext
    task: asyncio.Task[Any] | None = None
    outcome: dict[str, Any] | None = None
    waiters: int = 0


class AgentRegistry:
    # 创建会话内唯一的调度表，以同一条件锁协调树、收件箱和运行状态
    def __init__(self, session_id: str = "", bus: EventBus | None = None) -> None:
        self.session_id = session_id
        self.bus = bus or EventBus()
        self.agents: OrderedDict[str, AgentRuntime] = OrderedDict()
        self.runs: OrderedDict[str, RunRecord] = OrderedDict()
        self.condition = asyncio.Condition()
        self.closed = False

    # 登记主 agent；调用方持有 Session 用户输入锁
    def ensure_main(self, messages: list[dict[str, Any]] | None = None) -> AgentRuntime:
        if "main" not in self.agents:
            history = messages if messages is not None else []
            self.agents["main"] = AgentRuntime(
                AgentContext(
                    session_id=self.session_id,
                    description="Main agent",
                    messages=history,
                    persisted_count=len(history),
                )
            )
        return self.agents["main"]

    # 获取本会话中的目标，拒绝不存在或已淘汰的标识
    def _agent(self, agent_id: str) -> AgentRuntime:
        if agent_id not in self.agents:
            raise ValueError("Agent does not exist or has expired")
        return self.agents[agent_id]

    # 判断目标是否属于调用者的后代，主 agent 不属于任何子树
    def _descendant(self, target: str, caller: str) -> bool:
        parent = self._agent(target).context.parent_agent_id
        while parent is not None:
            if parent == caller:
                return True
            ancestor = self.agents.get(parent)
            parent = ancestor.context.parent_agent_id if ancestor else None
        return False

    # 生成不含完整历史的状态快照
    def snapshot(self, agent_id: str) -> dict[str, Any]:
        agent = self._agent(agent_id)
        latest = self.runs.get(agent.latest_run_id or "")
        return {
            "agent_id": agent_id,
            "parent_agent_id": agent.context.parent_agent_id,
            "description": agent.context.description,
            "state": agent.state,
            "run_id": agent.current.run_id if agent.current else None,
            "latest_run_id": agent.latest_run_id,
            "pending_messages": len(agent.inbox),
            "discarded_messages": agent.discarded_messages,
            "close_reason": "agent_closed" if agent.state == "CLOSED" else None,
            "result": dict(latest.outcome) if latest and latest.outcome else None,
        }

    # 发布快照时隔离观察者异常，避免 UI 故障破坏运行状态
    async def _notify(self, agent_id: str) -> None:
        try:
            await self.bus.publish(
                AgentStateEvent(
                    session_id=self.session_id,
                    agent_id=agent_id,
                    snapshot=self.snapshot(agent_id),
                    ts=datetime.now(UTC).isoformat(),
                )
            )
        except Exception:
            logging.getLogger(__name__).exception("agent state observer failed")

    # 发布消息接收、消费或关闭丢弃状态
    async def _message_event(
        self,
        agent_id: str,
        message: AgentMessage,
        status: str,
    ) -> None:
        try:
            await self.bus.publish(
                AgentMessageEvent(
                    session_id=self.session_id,
                    agent_id=agent_id,
                    message_id=message.message_id,
                    sender_agent_id=message.sender,
                    status=status,
                    ts=datetime.now(UTC).isoformat(),
                )
            )
        except Exception:
            logging.getLogger(__name__).exception("agent message observer failed")

    # 锁内把待处理消息一次性加入所属 agent 历史
    def _drain(self, agent: AgentRuntime) -> list[AgentMessage]:
        messages = []
        while agent.inbox:
            message = agent.inbox.popleft()
            messages.append(message)
            agent.context.add_user_message(
                "[Agent message]\n"
                + json.dumps(
                    {
                        "message_id": message.message_id,
                        "sender": message.sender,
                        "message": message.content,
                    },
                    ensure_ascii=False,
                ),
            )
        return messages

    # 主输入先接收积压协作消息再追加用户输入，调用方持有会话锁
    async def prepare_input(self, content: str) -> None:
        async with self.condition:
            agent = self.ensure_main()
            if self.closed or agent.state != "IDLE":
                raise ValueError("Main agent is busy or closed")
            messages = self._drain(agent)
            agent.context.add_user_message(content)
        for message in messages:
            await self._message_event("main", message, "delivered")

    # 在主执行或空闲维护的独占路径中消费消息
    async def consume(self, context: ExecutionContext, *, finishing: bool = False) -> bool:
        agent_id = context.agent_context.agent_id
        async with self.condition:
            agent = self._agent(agent_id)
            if agent.state in {"CLOSING", "CLOSED"}:
                raise asyncio.CancelledError()
            if agent.current is not context or not agent.accepting:
                return False
            messages: list[AgentMessage] = []
            if agent.inbox and context.has_steps_remaining():
                messages = self._drain(agent)
            elif finishing and agent.inbox:
                context.mark_failed("exceeded_max_steps")
            if finishing and not messages:
                agent.accepting = False
        for message in messages:
            await self._message_event(agent_id, message, "delivered")
        return bool(messages)

    # 为主执行登记占用状态，直到保存和清理完成才释放
    async def begin_main(self, context: ExecutionContext) -> None:
        async with self.condition:
            agent = self.ensure_main()
            if self.closed or agent.state != "IDLE":
                raise ValueError("Main agent is busy or closed")
            if context.run_id in self.runs:
                raise ValueError("Run ID already exists")
            self._begin(agent, context, asyncio.current_task())
        await self._notify("main")

    # 锁内建立新的运行记录
    def _begin(
        self,
        agent: AgentRuntime,
        context: ExecutionContext,
        task: asyncio.Task[Any] | None,
    ) -> None:
        agent.current = context
        agent.task = task
        agent.state = "RUNNING"
        agent.accepting = True
        agent.latest_run_id = context.run_id
        self.runs[context.run_id] = RunRecord(context, task)
        self.condition.notify_all()

    # 锁内创建子执行任务，所有初始化也处于可被关闭追踪的任务内
    def _start(self, agent: AgentRuntime, goal: str) -> ExecutionContext:
        context = ExecutionContext(
            new_run_id(),
            goal,
            agent.max_steps,
            agent_context=agent.context,
            registry=self,
            system_prompt_override=agent.context.profile.system_prompt
            if agent.context.profile
            else None,
        )
        task = asyncio.create_task(self._drive(agent, context))
        self._begin(agent, context, task)
        return context

    # 在同一个临界区检查父节点、深度和容量并登记新 agent
    async def spawn(
        self,
        caller: str,
        description: str,
        prompt: str,
        profile: AgentProfile | None,
        execute: Executor,
        max_steps: int | None,
        runs_dir: Path,
    ) -> tuple[str, str]:
        async with self.condition:
            parent = self._agent(caller)
            if self.closed or parent.state in {"CLOSING", "CLOSED"}:
                raise ValueError("Parent agent is closing")
            if parent.depth >= 2:
                raise ValueError("Subagent nesting limit (2) reached")
            if sum(a.state != "CLOSED" for key, a in self.agents.items() if key != "main") >= 6:
                raise ValueError("Subagent capacity (6) reached; close an idle agent first")
            agent_id = f"agent-{uuid4().hex}"
            agent = AgentRuntime(
                AgentContext(
                    agent_id,
                    self.session_id,
                    caller,
                    description,
                    profile,
                    [{"role": "user", "content": prompt}],
                ),
                depth=parent.depth + 1,
                state="CREATING",
                execute=execute,
                max_steps=normalize_max_steps(max_steps),
            )
            self.agents[agent_id] = agent
            context = self._start(agent, prompt)
            agent.tasks_dir = runs_dir / context.run_id / ".tasks"
        await self._notify(agent_id)
        return agent_id, context.run_id

    # 执行已登记的任务，异常和取消也必须提交终态并唤醒等待者
    async def _drive(self, agent: AgentRuntime, context: ExecutionContext) -> None:
        try:
            if agent.state == "CLOSING":
                raise asyncio.CancelledError()
            assert agent.execute is not None
            await agent.execute(context)
        except asyncio.CancelledError:
            context.mark_failed("cancelled")
        except Exception:
            logging.getLogger(__name__).exception("agent execution failed")
            context.mark_failed("execution_error")
        finally:
            context.repair_tool_results()
            await self.finish(context)

    # 提交固定的运行结果，成功收尾后才处理期间到达的唤醒请求
    async def finish(self, context: ExecutionContext) -> None:
        agent_id = context.agent_context.agent_id
        async with self.condition:
            record = self.runs[context.run_id]
            record.outcome = self._outcome(context)
            record.task = None
            agent = self._agent(agent_id)
            if agent.current is context:
                agent.current = None
                agent.task = None
                agent.accepting = False
                if agent.state != "CLOSING":
                    agent.state = "IDLE"
                    if (
                        agent_id != "main"
                        and context.status == "success"
                        and any(m.wake for m in agent.inbox)
                        and not self.closed
                    ):
                        self._start(agent, agent.inbox[-1].content)
            self._trim()
            self.condition.notify_all()
        await self._notify(agent_id)

    # 从执行上下文生成独立结果，后续轮次不会覆盖历史结果
    @staticmethod
    def _outcome(context: ExecutionContext) -> dict[str, Any]:
        return {
            "agent_id": context.agent_context.agent_id,
            "run_id": context.run_id,
            "status": context.status,
            "reason": context.reason,
            "result": context.result,
            "steps": context.step,
        }

    # 淘汰未被活跃 agent 或等待操作引用的旧终态记录
    def _trim(self) -> None:
        protected = {a.latest_run_id for a in self.agents.values() if a.state != "CLOSED"}
        completed = [key for key, value in self.runs.items() if value.outcome is not None]
        count = len(completed)
        for key in completed:
            if count <= 256:
                break
            if key not in protected and not self.runs[key].waiters:
                del self.runs[key]
                count -= 1
        closed = [key for key, a in self.agents.items() if a.state == "CLOSED"]
        for key in closed[:-256]:
            del self.agents[key]

    # 接受消息并仅在目标空闲时显式启动新执行
    async def send(
        self,
        sender: str,
        target: str,
        content: str,
        wake: bool = False,
    ) -> dict[str, Any]:
        if not content.strip() or len(content.encode("utf-8")) > 16 * 1024:
            raise ValueError("Message must contain 1..16384 UTF-8 bytes")
        if sender == target or (target == "main" and wake):
            raise ValueError("Cannot send to self or automatically wake main")
        async with self.condition:
            if sender != "user" and self._agent(sender).state in {"CLOSING", "CLOSED"}:
                raise ValueError("Sender is closing")
            agent = self._agent(target)
            if self.closed or agent.state in {"CLOSING", "CLOSED"}:
                raise ValueError("Target is closing or closed")
            if len(agent.inbox) >= 64:
                raise ValueError("Inbox is full (64 messages)")
            message = AgentMessage(uuid4().hex, sender, content, wake)
            agent.inbox.append(message)
            if wake and agent.state == "IDLE":
                self._start(agent, content)
            self.condition.notify_all()
            result = {"message_id": message.message_id, "accepted": True, **self.snapshot(target)}
        await self._message_event(target, message, "accepted")
        await self._notify(target)
        return result

    # 查询 agent 或固定 Run；空查询返回会话内身份列表
    def result(self, agent_id: str | None = None, run_id: str | None = None) -> dict[str, Any]:
        if agent_id and run_id:
            raise ValueError("Specify either agent_id or run_id")
        if agent_id:
            return self.snapshot(agent_id)
        if run_id:
            record = self.runs.get(run_id)
            if record is None:
                raise ValueError("Unknown run_id: result does not exist or has expired")
            if record.outcome is not None:
                return dict(record.outcome)
            if record.task is not None and record.task.done():
                if record.task.cancelled():
                    record.context.mark_failed("cancelled")
                elif record.task.exception() is not None:
                    record.context.mark_failed("execution_error")
                return self._outcome(record.context)
            return {
                "agent_id": record.context.agent_context.agent_id,
                "run_id": run_id,
                "status": "running",
                "result": "still running",
            }
        return {"agents": [self.snapshot(key) for key in self.agents]}

    # 固定目标 Run 并等待共享条件，始终只观察收件箱
    async def wait(
        self,
        caller: str,
        agent_ids: list[str] | None = None,
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        if not 0 <= timeout_ms <= 60000:
            raise ValueError("timeout_ms must be between 0 and 60000")
        async with self.condition:
            source = self._agent(caller)
            targets = [self._agent(key) for key in dict.fromkeys(agent_ids or [])]
            if any(not self._descendant(a.context.agent_id, caller) for a in targets):
                raise ValueError("Only descendant runs may be waited on")
            records = [
                self.runs[a.latest_run_id]
                for a in targets
                if a.latest_run_id is not None and a.latest_run_id in self.runs
            ]
            for record in records:
                record.waiters += 1

            # 状态检查和注册等待共享同一条件锁，避免丢失通知
            def ready() -> bool:
                return (
                    bool(source.inbox)
                    or source.state in {"CLOSING", "CLOSED"}
                    or any(r.outcome is not None for r in records)
                    or any(a.state == "CLOSED" for a in targets)
                )

            try:
                timed_out = False
                if not ready():
                    try:
                        async with asyncio.timeout(timeout_ms / 1000):
                            await self.condition.wait_for(ready)
                    except TimeoutError:
                        timed_out = True
                return {
                    "reason": "timeout"
                    if timed_out
                    else "message_received"
                    if source.inbox
                    else "state_changed",
                    "pending_messages": len(source.inbox),
                    "runs": [self.result(run_id=r.context.run_id) for r in records],
                }
            finally:
                for record in records:
                    record.waiters -= 1
                self._trim()

    # 前台等待指定执行，调用者取消不会向目标传播取消
    async def join(self, run_id: str) -> dict[str, Any]:
        async with self.condition:
            record = self.runs[run_id]
            record.waiters += 1
            try:
                await self.condition.wait_for(lambda: record.outcome is not None)
                return dict(record.outcome or {})
            finally:
                record.waiters -= 1

    # 原子标记子树关闭，再由独立清理任务取消并等待全部子任务
    async def close(self, caller: str, target: str) -> dict[str, Any]:
        async with self.condition:
            agent = self._agent(target)
            if target == "main" or not self._descendant(target, caller):
                raise ValueError("Only descendants can be closed")
            if agent.state == "CLOSED":
                return self.snapshot(target)
            if agent.close_task is None:
                subtree = [
                    a
                    for key, a in self.agents.items()
                    if a.state != "CLOSED" and (key == target or self._descendant(key, target))
                ]
                ongoing = {a.close_task for a in subtree if a.close_task is not None}
                for item in subtree:
                    item.state = "CLOSING"
                    item.accepting = False
                task = asyncio.create_task(self._close_tree(subtree, list(ongoing)))
                for item in subtree:
                    item.close_task = task
                self.condition.notify_all()
            closing = agent.close_task
        assert closing is not None
        await asyncio.shield(closing)
        return self.snapshot(target)

    # 在管理锁外完成取消，终态提交后释放历史和消息
    async def _close_tree(
        self,
        subtree: list[AgentRuntime],
        ongoing: list[asyncio.Task[None]],
    ) -> None:
        tasks = [a.task for a in subtree if a.task is not None]
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks or ongoing:
            await asyncio.gather(*tasks, *ongoing, return_exceptions=True)
        discarded: list[tuple[str, AgentMessage]] = []
        async with self.condition:
            for agent in subtree:
                if agent.current is not None:
                    context = agent.current
                    context.mark_failed("cancelled")
                    record = self.runs[context.run_id]
                    record.outcome = self._outcome(context)
                    record.task = None
                discarded.extend((agent.context.agent_id, m) for m in agent.inbox)
                agent.discarded_messages += len(agent.inbox)
                agent.inbox.clear()
                agent.context.messages.clear()
                agent.current = None
                agent.task = None
                agent.execute = None
                agent.state = "CLOSED"
            self.condition.notify_all()
        for agent_id, message in discarded:
            await self._message_event(agent_id, message, "discarded")
        for agent in subtree:
            await self._notify(agent.context.agent_id)
        self._trim()

    # 停止接收新任务并关闭所有子树，供会话离开和 Core 退出复用
    async def shutdown(self) -> None:
        async with self.condition:
            self.closed = True
            children = [
                key
                for key, a in self.agents.items()
                if a.context.parent_agent_id == "main" and a.state != "CLOSED"
            ]
        for key in children:
            await self.close("main", key)

    # 兼容原有后台任务检查接口，状态来源仍为同一个 Run 索引
    def register(
        self,
        run_id: str,
        task: asyncio.Task[None],
        context: ExecutionContext,
    ) -> None:
        self.runs[run_id] = RunRecord(context, task)

    # 兼容旧调用方按运行标识取得任务
    def get(self, run_id: str) -> tuple[asyncio.Task[Any], ExecutionContext] | None:
        record = self.runs.get(run_id)
        if record is not None and record.task is not None:
            return record.task, record.context
        return None

    # 返回仍持有任务的运行记录供现有 busy 检查使用
    def all(self) -> list[tuple[asyncio.Task[Any], ExecutionContext]]:
        return [(r.task, r.context) for r in self.runs.values() if r.task is not None]

    # 检查所有创建、执行及清理操作是否已经结束
    def is_busy(self) -> bool:
        return any(a.state in {"CREATING", "RUNNING", "CLOSING"} for a in self.agents.values())


BackgroundTaskRegistry = AgentRegistry
