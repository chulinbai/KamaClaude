from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from kama_claude.core.agents.loader import AgentProfile
from kama_claude.core.app import CoreApp
from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.config import KamaConfig
from kama_claude.core.context import AgentContext, ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.mcp.client import McpClient, McpToolDef
from kama_claude.core.mcp.tool import McpTool
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.subagent.registry import AgentRegistry
from kama_claude.core.subagent.tool import SendInputTool
from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry


# 构造真实会话与执行内核，仅替换外部模型服务
def _manager(path: Path, provider: Any) -> tuple[SessionManager, SessionStore]:
    bus = EventBus()
    store = SessionStore(path / "sessions")
    config = KamaConfig()
    config.compaction.auto_threshold = 0.8
    manager = SessionManager(
        store,
        lambda: AgentRunner(config, bus=bus, provider=provider),
        bus,
        provider=provider,
        workspace=path,
    )
    return manager, store


# 功能：取消工具批次保留完成结果并修复未完成调用，下一轮输入历史仍完整
# 设计：在第二个工具入口用事件阻塞，取消真实 SessionManager 执行后检查磁盘和下一次模型输入
async def test_cancel_tool_batch_repairs_and_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    provider = Mock(
        chat=AsyncMock(
            side_effect=[
                LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCallBlock(id="a", name="first", input={}),
                        ToolCallBlock(id="b", name="second", input={}),
                        ToolCallBlock(id="c", name="third", input={}),
                    ],
                ),
                LlmResponse(stop_reason="end_turn", text="continued"),
            ]
        )
    )
    calls: list[str] = []

    # 第一项完成后在第二项阻塞，第三项不应开始
    async def invoke(registry, call, bus, run_id, **kwargs):
        calls.append(call.id)
        if call.id == "a":
            return ToolResult(content="first completed")
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("kama_claude.core.loop.invoke_tool", invoke)
    manager, store = _manager(tmp_path, provider)
    sid = (await manager.create("chat")).id
    task = asyncio.create_task(manager.send_message(sid, "start"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["a", "b"]
    history = store.read_messages(sid)
    results = history[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b", "c"]
    assert results[0]["content"] == "first completed"
    assert all(r["is_error"] and "unknown" in r["content"] for r in results[1:])
    assert not manager.is_busy(sid)
    await manager.send_message(sid, "continue")
    live = manager.agent_registry(sid).ensure_main().context.messages
    assert live == store.read_messages(sid)
    assert live[2] == history[2]
    await manager.shutdown()


# 功能：自动压缩替换旧历史后仍保存摘要和后续消息，手动压缩也同步内存并可恢复
# 设计：预填历史比压缩结果更长，专门覆盖旧 prefill_len 导致新增内容漏存的路径
async def test_compaction_replaces_live_and_persisted_history(tmp_path: Path) -> None:
    observed: list[list[dict[str, Any]]] = []

    # 普通执行先请求工具触发压缩，摘要请求与后续调用分别返回固定文本
    async def chat(**kwargs):
        if kwargs["run_id"] == "compact":
            return LlmResponse(stop_reason="end_turn", text="short summary")
        observed.append(deepcopy(kwargs["messages"]))
        if kwargs["step"] == 1 and len(observed) == 1:
            return LlmResponse(
                stop_reason="tool_use",
                tool_calls=[ToolCallBlock(id="u", name="missing", input={})],
                usage=UsageStats(100, 20, 0, 0, 0.95),
            )
        return LlmResponse(stop_reason="end_turn", text="answer after summary")

    manager, store = _manager(tmp_path, Mock(chat=chat))
    sid = (await manager.create("chat")).id
    for _ in range(5):
        store.append_message(sid, "user", "long previous request")
        store.append_message(sid, "assistant", "long previous response")
    await manager.send_message(sid, "continue", run_id="auto-compact")
    agent = manager.agent_registry(sid).ensure_main().context
    assert len(agent.messages) == 3
    assert agent.messages[0]["content"] == "short summary"
    assert store.read_messages(sid) == agent.messages
    assert observed[1][0]["content"] == "short summary"
    await manager.compact(sid)
    assert len(agent.messages) == 2
    assert store.read_messages(sid) == agent.messages
    await manager.send_message(sid, "after manual")
    assert observed[-1][0]["content"] == "short summary"
    await manager.close(sid)
    await manager.resume(sid)
    assert manager.agent_registry(sid).ensure_main().context.messages == store.read_messages(sid)
    await manager.shutdown()


# 功能：保存部分成功后重试使用完整原子替换，不重复追加也不丢失尾部消息
# 设计：故意让第二次单条追加失败，验证磁盘部分提交和运行期脏状态的恢复行为
def test_partial_save_failure_retries_full_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    agent = AgentContext(
        session_id="s",
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ],
    )
    original = store.append_message
    count = 0

    # 模拟第二条写入时发生磁盘错误
    def append(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("disk failure")
        original(*args, **kwargs)

    monkeypatch.setattr(store, "append_message", append)
    with pytest.raises(OSError):
        store.save_agent(agent, "first")
    assert agent.history_replaced
    assert len(store.read_messages("s")) == 1
    store.save_agent(agent, "retry")
    assert store.read_messages("s") == agent.messages
    assert not agent.history_replaced


# 功能：离开会话前重试脏历史，保存失败时不能丢弃唯一的运行期副本
# 设计：主执行结束后模拟待提交消息，关闭失败再重试，检查引用仍在且最终文件完整
async def test_close_retries_dirty_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = Mock(chat=AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="done")))
    manager, store = _manager(tmp_path, provider)
    sid = (await manager.create("chat")).id
    await manager.send_message(sid, "go")
    agent = manager.agent_registry(sid).ensure_main().context
    agent.messages.append({"role": "user", "content": "uncommitted"})
    original = store.append_messages
    monkeypatch.setattr(store, "append_messages", Mock(side_effect=OSError("disk failure")))
    with pytest.raises(OSError):
        await manager.close(sid)
    assert manager.agent_registry(sid).ensure_main().context is agent
    monkeypatch.setattr(store, "append_messages", original)
    await manager.close(sid)
    assert store.read_messages(sid) == agent.messages


# 功能：普通主轮结束保留后台线程，one-shot 结束等待后台任务清理后才关闭会话
# 设计：模型经真实 spawn 工具启动阻塞子线程，并在子线程 finally 中标记清理结果
@pytest.mark.parametrize("mode", ["chat", "one_shot"])
async def test_session_background_lifetime(tmp_path: Path, mode: str) -> None:
    started, cleaned = asyncio.Event(), asyncio.Event()
    manager: SessionManager
    sid = ""

    # 根据本轮身份区分主子调用，模拟主完成而后台仍在运行
    async def chat(**kwargs):
        registry = manager.agent_registry(sid)
        context = registry.runs[kwargs["run_id"]].context
        if context.agent_context.agent_id != "main":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        if kwargs["step"] == 1:
            return LlmResponse(
                stop_reason="tool_use",
                tool_calls=[
                    ToolCallBlock(
                        id="spawn",
                        name="spawn_agent",
                        input={
                            "description": "background",
                            "prompt": "child only",
                            "run_in_background": True,
                        },
                    )
                ],
            )
        await started.wait()
        return LlmResponse(stop_reason="end_turn", text="parent done")

    manager, store = _manager(tmp_path, Mock(chat=chat))
    sid = (await manager.create(mode)).id
    await manager.send_message(sid, "parent secret")
    if mode == "chat":
        registry = manager.agent_registry(sid)
        child = next(a for key, a in registry.agents.items() if key != "main")
        assert child.state == "RUNNING"
        assert not cleaned.is_set()
        assert child.context.messages == [{"role": "user", "content": "child only"}]
        assert registry is manager.agent_registry(sid)
        await manager.shutdown()
    else:
        assert store.read_meta(sid).status == "closed"
        assert sid not in manager._registries
    assert cleaned.is_set()


# 功能：子线程使用共用内核、MCP 和角色白名单，日志只包含本轮，主笔记不会泄漏
# 设计：先完成带 notes 的根调用，再用受限角色执行两轮，检查真实模型入参和每个事件文件
async def test_shared_kernel_role_mcp_and_log_isolation(tmp_path: Path) -> None:
    captured: list[dict[str, Any]] = []

    # 捕获每次模型实际可见的能力与系统提示
    async def chat(**kwargs):
        captured.append(deepcopy({k: v for k, v in kwargs.items() if k != "bus"}))
        return LlmResponse(stop_reason="end_turn", text="done")

    provider = Mock(chat=chat)
    manager, store = _manager(tmp_path, provider)
    sid = (await manager.create("chat")).id
    store.append_note(sid, "main private notes", "old")
    await manager.send_message(sid, "main private history", run_id="root")
    registry = manager.agent_registry(sid)
    client = Mock()
    mcp_tool = McpTool(client, "server", McpToolDef("read", "shared"))
    runner = AgentRunner(
        KamaConfig(),
        provider=provider,
        bus=manager._bus,
        mcp_manager=Mock(get_tools=Mock(return_value=[mcp_tool])),
    )
    runner.bind_registry(registry)
    role = AgentProfile("worker", "worker", "role instructions", ["server__read", "send_input"])

    # 使用相同内核运行子身份并保留统一日志路径
    async def execute(context: ExecutionContext) -> None:
        await runner.run_context(context, workspace=tmp_path, runs_dir=store.runs_dir(sid))

    agent, run = await registry.spawn("main", "worker", "cold start", role, execute, 3, tmp_path)
    await registry.join(run)
    second = (await registry.send("main", agent, "continue", wake=True))["run_id"]
    await registry.join(second)
    for call in captured[1:]:
        assert {t["name"] for t in call["tool_schemas"]} == {"server__read", "send_input"}
        assert "role instructions" in call["system"]
        assert "main private" not in str(call)
    for run_id, expected_agent in [("root", "main"), (run, agent), (second, agent)]:
        rows = [
            json.loads(line)
            for line in (store.runs_dir(sid) / run_id / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert rows[0]["type"] == "run.started"
        assert rows[-1]["type"] == "run.finished"
        assert all(row["run_id"] == run_id and row["agent_id"] == expected_agent for row in rows)
    await manager.shutdown()


# 功能：取消共享 MCP 的一个调用后，其迟到响应不会被另一个调用误用
# 设计：真实 StreamReader 注入先旧后新的响应，保留客户端原有串行锁和解析循环
async def test_shared_mcp_cancellation_and_late_response(monkeypatch: pytest.MonkeyPatch) -> None:
    client = McpClient()
    client._reader = asyncio.StreamReader()
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # 捕获请求并让测试按确定顺序投递响应
    async def write(line: str) -> None:
        sent.put_nowait(json.loads(line))

    monkeypatch.setattr(client, "_write_line", write)
    first = asyncio.create_task(client.call_tool("first", {}))
    req1 = await sent.get()
    second = asyncio.create_task(client.call_tool("second", {}))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    req2 = await sent.get()
    for request, text in [(req1, "late first"), (req2, "second result")]:
        client._reader.feed_data(
            (
                json.dumps(
                    {
                        "id": request["id"],
                        "result": {"content": [{"type": "text", "text": text}]},
                    }
                )
                + "\n"
            ).encode()
        )
    assert await asyncio.wait_for(second, 2) == "second result"
    assert not client._lock.locked()


# 功能：协作工具错误不自动重试，内部等待也不被普通工具的默认超时截断
# 设计：实际调用 invoke_tool 并令注册表发送失败，检查发送次数和 managed timeout 路径
async def test_coordination_invocation_no_retry_or_outer_timeout() -> None:
    registry = AgentRegistry("s")
    registry.send = AsyncMock(side_effect=ValueError("target is closing"))
    tools = ToolRegistry()
    tools.register(SendInputTool(registry, "main"))
    result = await invoke_tool(
        tools,
        ToolCallBlock(id="send", name="send_input", input={"agent_id": "a", "message": "hi"}),
        EventBus(),
        "r",
        timeout=0,
    )
    assert result.is_error
    assert "closing" in result.content
    registry.send.assert_awaited_once()


# 功能：工具批次执行期间投递的协作消息只在完整结果之后加入历史，且不重置步数
# 设计：在第一个工具中投递消息、第二个工具中检查历史，再验证模型下一步的真实输入顺序
async def test_running_message_consumed_after_complete_tool_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = Mock(
        chat=AsyncMock(
            side_effect=[
                LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCallBlock(id="a", name="first", input={}),
                        ToolCallBlock(id="b", name="second", input={}),
                    ],
                ),
                LlmResponse(stop_reason="end_turn", text="done"),
            ]
        )
    )
    manager, store = _manager(tmp_path, provider)
    sid = (await manager.create("chat")).id
    registry = manager.agent_registry(sid)

    # 在工具执行中使用同一注册表投递，验证正文不能穿插到工具结果之间
    async def invoke(tools, call, bus, run_id, **kwargs):
        if call.id == "a":
            await registry.send("user", "main", "while running")
        else:
            assert len(registry.agents["main"].inbox) == 1
            assert "while running" not in str(registry.agents["main"].context.messages)
        return ToolResult(content=f"result {call.id}")

    monkeypatch.setattr("kama_claude.core.loop.invoke_tool", invoke)
    await manager.send_message(sid, "start", run_id="batch")
    history = store.read_messages(sid)
    assert [block["tool_use_id"] for block in history[2]["content"]] == ["a", "b"]
    assert '"message": "while running"' in history[3]["content"]
    assert str(history).count("while running") == 1
    assert registry.result(run_id="batch")["steps"] == 2
    await manager.shutdown()


# 功能：Core 退出等待普通聊天和后台子任务收尾后才停止共享 MCP
# 设计：通过真实聊天 handler 启动根执行并直接启动子执行，用清理标记断言共享服务关闭顺序
async def test_core_shutdown_awaits_main_and_child_before_mcp(tmp_path: Path) -> None:
    entered, root_cleaned, child_cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    child_entered = asyncio.Event()

    # 模拟被取消时需要运行清理代码的主模型调用
    async def chat(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            root_cleaned.set()

    app = CoreApp()
    manager, _ = _manager(tmp_path, Mock(chat=chat))
    app._sessions = manager
    sid = (await manager.create("chat")).id
    root = asyncio.create_task(app._session_send_handler({"session_id": sid, "content": "start"}))
    await entered.wait()
    registry = manager.agent_registry(sid)

    # 模拟共享服务仍被子执行使用的窗口
    async def execute(context: ExecutionContext) -> None:
        child_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            child_cleaned.set()

    await registry.spawn("main", "child", "work", None, execute, 3, tmp_path)
    await child_entered.wait()

    # MCP 停止时两个清理标记必须已提交
    async def stop_mcp() -> None:
        assert root_cleaned.is_set() and child_cleaned.is_set()
        assert not registry.is_busy()

    app._mcp_manager = Mock(stop_all=AsyncMock(side_effect=stop_mcp))
    server = Mock(stop=AsyncMock())
    await app._shutdown(server)
    assert root.cancelled()
    assert not app._running_runs
    app._mcp_manager.stop_all.assert_awaited_once()
    server.stop.assert_awaited_once()
    with pytest.raises(HandlerError, match="shutting down"):
        await manager.create("chat")
