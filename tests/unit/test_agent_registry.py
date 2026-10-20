from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.context import ExecutionContext
from kama_claude.core.subagent.registry import AgentRegistry, Executor


# 创建带主身份的注册表
def _registry() -> AgentRegistry:
    registry = AgentRegistry("test-session")
    registry.ensure_main()
    return registry


# 使用真实消费入口完成一轮，以便测试跨轮历史与调度
async def _complete(context: ExecutionContext) -> None:
    assert context.registry is not None
    await context.registry.consume(context)
    context.step += 1
    context.add_assistant_message([{"type": "text", "text": context.goal}])
    context.result = context.goal
    context.mark_success()


# 创建指定父身份的逻辑子线程
async def _spawn(
    registry: AgentRegistry,
    path: Path,
    execute: Executor = _complete,
    caller: str = "main",
) -> tuple[str, str]:
    return await registry.spawn(caller, "worker", "first", None, execute, 3, path)


# 功能：追加任务复用身份和历史但重新计步，旧执行结果不随最新执行变化
# 设计：先排队不唤醒的消息，再显式追加任务，验证 FIFO、单份历史及固定结果索引
async def test_reuse_history_and_fixed_run_result(tmp_path: Path) -> None:
    registry = _registry()
    agent_id, first = await _spawn(registry, tmp_path)
    await registry.join(first)
    runtime = registry.agents[agent_id]
    history = runtime.context.messages
    tasks_dir = runtime.tasks_dir
    await registry.send("main", agent_id, "queued")
    assert runtime.state == "IDLE"
    assert len(history) == 2
    accepted = await registry.send("main", agent_id, "next", wake=True)
    second = accepted["run_id"]
    await registry.join(second)
    assert second != first
    assert runtime.context.messages is history
    assert runtime.tasks_dir == tasks_dir
    assert len(history) == 5
    assert '"message": "queued"' in history[2]["content"]
    assert '"message": "next"' in history[3]["content"]
    assert registry.result(run_id=first)["result"] == "first"
    assert registry.result(run_id=second)["steps"] == 1
    await registry.shutdown()


# 功能：兄弟消息只进入目标收件箱，等待不消费，主身份不能被自动唤醒
# 设计：三个独立历史交叉投递，再通过下一次主用户输入核对主收件箱的 FIFO 顺序
async def test_inbox_isolation_wait_and_main_input(tmp_path: Path) -> None:
    registry = _registry()
    children = [await _spawn(registry, tmp_path) for _ in range(3)]
    for _, run in children:
        await registry.join(run)
    a, b, c = [pair[0] for pair in children]
    await registry.send(a, b, "only B")
    for _ in range(2):
        observed = await registry.wait(b, timeout_ms=0)
        assert observed["reason"] == "message_received"
        assert observed["pending_messages"] == 1
    assert len(registry.agents[b].context.messages) == 2
    assert not registry.agents[c].inbox
    with pytest.raises(ValueError, match="descendant"):
        await registry.wait(a, [b], 0)
    with pytest.raises(ValueError, match="wake main"):
        await registry.send(a, "main", "report", wake=True)
    await registry.send(a, "main", "report 1")
    await registry.send(b, "main", "report 2")
    await registry.prepare_input("user input")
    history = registry.agents["main"].context.messages
    assert '"message": "report 1"' in history[0]["content"]
    assert '"message": "report 2"' in history[1]["content"]
    assert history[2]["content"] == "user input"
    assert not registry.agents["main"].inbox
    await registry.shutdown()


# 功能：运行中的消息沿用本轮步数，收尾后接受的唤醒请求只在旧执行结束后启动
# 设计：用终结检查后的两个事件固定竞态，并记录同时运行数以排除双重启动
async def test_wake_during_finalization_starts_one_followup(tmp_path: Path) -> None:
    registry = _registry()
    finalizing, release = asyncio.Event(), asyncio.Event()
    active = 0
    peak = 0
    contexts: list[ExecutionContext] = []

    # 固定第一轮进入收尾但尚未提交结果的时间窗口
    async def execute(context: ExecutionContext) -> None:
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        contexts.append(context)
        await registry.consume(context)
        context.step = 1
        if len(contexts) == 1:
            assert not await registry.consume(context, finishing=True)
            finalizing.set()
            await release.wait()
        context.mark_success()
        active -= 1

    agent, first = await _spawn(registry, tmp_path, execute)
    await finalizing.wait()
    waiting = asyncio.Event()

    # 发出标记后直接进入等待，直到释放条件锁才允许测试线程继续
    async def wait_first() -> dict[str, Any]:
        waiting.set()
        return await registry.wait("main", [agent])

    waiter = asyncio.create_task(wait_first())
    await waiting.wait()
    assert registry.runs[first].waiters == 1
    await registry.send("main", agent, "follow-up", wake=True)
    assert len(contexts) == 1
    release.set()
    observed = await asyncio.wait_for(waiter, 2)
    assert observed["runs"][0]["run_id"] == first
    latest = registry.agents[agent].latest_run_id
    assert latest and latest != first
    await registry.join(latest)
    assert peak == 1
    assert len(contexts) == 2
    assert contexts[0].step == 1
    assert contexts[0].messages is contexts[1].messages
    assert len(contexts[1].messages) == 2
    await registry.shutdown()


# 功能：失败和步数耗尽不因待处理 wake 消息而自动重启
# 设计：在最后一步注入消息，分别触发错误和预算边界，确认消息保留且显式追加后可继续
@pytest.mark.parametrize("reason", ["llm_error", "exceeded_max_steps"])
async def test_failed_run_keeps_pending_wake(tmp_path: Path, reason: str) -> None:
    registry = _registry()
    entered, release = asyncio.Event(), asyncio.Event()

    # 到达预算边界后等待测试投递消息
    async def execute(context: ExecutionContext) -> None:
        context.step = context.max_steps
        entered.set()
        await release.wait()
        if reason == "exceeded_max_steps":
            await registry.consume(context, finishing=True)
        else:
            context.mark_failed(reason)

    agent, run = await _spawn(registry, tmp_path, execute)
    await entered.wait()
    await registry.send("main", agent, "late input", wake=True)
    release.set()
    result = await registry.join(run)
    assert result["reason"] == reason
    assert registry.agents[agent].state == "IDLE"
    assert registry.agents[agent].latest_run_id == run
    assert len(registry.agents[agent].inbox) == 1
    assert len(registry.agents[agent].context.messages) == 1
    await registry.shutdown()


# 功能：等待超时或取消不会取消目标，消息到达可唤醒等待者且不消费正文
# 设计：目标执行用独立事件阻塞，先超时再取消 join，最后由真实发送唤醒 Inbox 等待
async def test_wait_timeout_cancel_and_notification(tmp_path: Path) -> None:
    registry = _registry()
    release = asyncio.Event()

    # 保持执行直到测试明确释放
    async def execute(context: ExecutionContext) -> None:
        await release.wait()
        context.mark_success()

    agent, run = await _spawn(registry, tmp_path, execute)
    assert (await registry.wait("main", [agent], 0))["reason"] == "timeout"
    join = asyncio.create_task(registry.join(run))
    join.cancel()
    await asyncio.gather(join, return_exceptions=True)
    assert registry.result(run_id=run)["status"] == "running"
    waiter = asyncio.create_task(registry.wait("main"))
    await registry.send(agent, "main", "done soon")
    assert (await asyncio.wait_for(waiter, 2))["reason"] == "message_received"
    assert len(registry.agents["main"].inbox) == 1
    release.set()
    await registry.join(run)
    await registry.shutdown()


# 功能：容量包含空闲身份且并发创建不会超限，深度和队列字节限制严格生效
# 设计：同时请求七次创建并使用多字节正文触发边界，关闭后重用容量而不重用身份
async def test_capacity_depth_and_inbox_limits(tmp_path: Path) -> None:
    registry = _registry()
    spawned = await asyncio.gather(
        *(_spawn(registry, tmp_path) for _ in range(7)),
        return_exceptions=True,
    )
    accepted = [result for result in spawned if isinstance(result, tuple)]
    assert len(accepted) == 6
    assert sum(isinstance(result, ValueError) for result in spawned) == 1
    for _, run in accepted:
        await registry.join(run)
    target = accepted[0][0]
    for i in range(64):
        await registry.send("main", target, str(i))
    with pytest.raises(ValueError, match="full"):
        await registry.send("main", target, "overflow")
    with pytest.raises(ValueError, match="UTF-8"):
        await registry.send("main", accepted[1][0], "中" * 5462)
    await registry.close("main", accepted[-1][0])
    grandchild, run = await _spawn(registry, tmp_path, caller=target)
    await registry.join(run)
    with pytest.raises(ValueError, match="nesting"):
        await _spawn(registry, tmp_path, caller=grandchild)
    await registry.shutdown()


# 功能：并发关闭父子树等待清理完成，拒绝新后代，保留兄弟任务并报告丢弃消息
# 设计：取消处理器中设置屏障，刻意重叠两次关闭，验证没有二次取消打断清理
async def test_close_nested_waits_cleanup_and_discards(tmp_path: Path) -> None:
    registry = _registry()
    cleaning, release, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events: list[Any] = []

    # 收集消息状态通知以验证关闭丢弃是可观察的
    async def collect(event: Any) -> None:
        events.append(event)

    # 模拟必须完整等待的异步资源清理
    async def execute(context: ExecutionContext) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    registry.bus.subscribe(collect)
    parent, run = await _spawn(registry, tmp_path)
    await registry.join(run)
    child, child_run = await _spawn(registry, tmp_path, execute, parent)
    sibling, sibling_run = await _spawn(registry, tmp_path)
    await registry.join(sibling_run)
    await started.wait()
    await registry.send(sibling, child, "queued")
    closing_child = asyncio.create_task(registry.close(parent, child))
    await cleaning.wait()
    closing_parent = asyncio.create_task(registry.close("main", parent))
    async with registry.condition:
        await registry.condition.wait_for(lambda: registry.agents[parent].state == "CLOSING")
    with pytest.raises(ValueError, match="closing"):
        await _spawn(registry, tmp_path, caller=parent)
    assert not closing_parent.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(closing_parent, closing_child), 2)
    assert registry.result(run_id=child_run)["reason"] == "cancelled"
    assert registry.agents[child].context.messages == []
    assert registry.agents[child].execute is None
    assert registry.agents[sibling].state == "IDLE"
    discarded = [e for e in events if e.type == "subagent.message" and e.status == "discarded"]
    assert len(discarded) == 1
    assert registry.snapshot(child)["discarded_messages"] == 1
    assert registry.snapshot(child)["close_reason"] == "agent_closed"
    assert (await registry.close("main", parent))["state"] == "CLOSED"
    await registry.shutdown()


# 功能：创建后立刻关闭也产生可查询终态，不留下未开始的 Task 或容量占用
# 设计：不向新任务让出执行权就调用关闭，覆盖协程入口前取消的特殊路径
async def test_close_immediately_after_spawn(tmp_path: Path) -> None:
    registry = _registry()
    agent, run = await _spawn(registry, tmp_path)
    await registry.close("main", agent)
    assert registry.result(run_id=run)["status"] != "running"
    assert not registry.is_busy()
    assert not registry.all()
    await registry.shutdown()


# 功能：执行结果有界缓存且活跃身份最近结果不被淘汰，过期查询明确报错
# 设计：让同一个身份完成超过缓存上限的轮次，避免只覆盖大量关闭身份的特例
async def test_completed_result_retention(tmp_path: Path) -> None:
    registry = _registry()
    agent, first = await _spawn(registry, tmp_path)
    await registry.join(first)
    for i in range(258):
        accepted = await registry.send("main", agent, f"task {i}", wake=True)
        await registry.join(accepted["run_id"])
    assert len(registry.runs) == 256
    with pytest.raises(ValueError, match="expired"):
        registry.result(run_id=first)
    assert registry.result(agent_id=agent)["result"]["result"] == "task 257"
    await registry.shutdown()
