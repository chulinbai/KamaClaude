from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import TypeAdapter

from kama_claude.core.bus.commands import Command, SessionListCommand, SessionResumeCommand
from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.bus.events import SessionMessageReceivedEvent
from kama_claude.core.config import KamaConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.paths import PathAccessController
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import (
    SESSION_BUSY,
    SESSION_INVALID,
    SESSION_NOT_FOUND,
    SESSION_NOT_RESUMABLE,
    SESSION_PROJECT_MISMATCH,
    SessionManager,
)
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.builtin import ReadFileTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry


# 构造真实运行器和固定响应的模型，避免网络及模型输出不确定性
def _manager(store: SessionStore, workspace: Path, provider: Mock | None = None) -> SessionManager:
    provider = provider or Mock(chat=AsyncMock(return_value=LlmResponse(
        stop_reason="end_turn", text="done",
    )))
    return SessionManager(
        store, lambda: AgentRunner(KamaConfig(), provider=provider), EventBus(), workspace=workspace,
    )


# 功能：重启会话管理器后恢复同一会话，下一次真实运行读取旧历史及笔记
# 设计：使用真实 Runner 和磁盘存储，仅替换模型响应，验证上下文输入及历史追加没有重复
async def test_resume_after_restart_keeps_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = SessionStore(tmp_path / "sessions")
    first = _manager(store, tmp_path)
    session = await first.create("chat", "existing conversation")
    old_run = await first.send_message(session.id, "remember the original request")
    store.append_note(session.id, "durable fact: port 5433", old_run)
    await first.close(session.id)
    history_before = store.read_messages(session.id)
    provider = Mock(chat=AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="continued")))
    restarted = _manager(store, tmp_path, provider)

    restored = await restarted.resume(session.id)

    assert restored.status == "waiting_for_input"
    assert restored.id == session.id
    assert restored.run_ids == [old_run]
    assert store.read_messages(session.id) == history_before
    provider.chat.assert_not_called()
    await restarted.send_message(session.id, "continue here")
    call = provider.chat.call_args.kwargs
    assert call["messages"][:2] == history_before
    assert call["messages"][2] == {"role": "user", "content": "continue here"}
    assert "durable fact: port 5433" in call["system"]
    assert len(store.read_messages(session.id)) == 4
    assert len(store.read_meta(session.id).run_ids) == 2
    assert len(store.list_meta()) == 1


# 功能：压缩后恢复只使用当前摘要，保留笔记且不展开历史备份
# 设计：保存独特原文与摘要，恢复后调用真实 Runner，检查传给模型的内容
async def test_resume_compacted_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = SessionStore(tmp_path / "sessions")
    manager = _manager(store, tmp_path)
    session = await manager.create("chat")
    store.append_message(session.id, "user", "original text only in backup")
    store.append_note(session.id, "separate memory", "old-run")
    store.write_compacted(session.id, [
        {"role": "user", "content": "compact summary"},
        {"role": "assistant", "content": "ack"},
    ])
    provider = Mock(chat=AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="done")))
    restarted = _manager(store, tmp_path, provider)
    await restarted.resume(session.id)
    await restarted.send_message(session.id, "next")
    call = provider.chat.call_args.kwargs
    assert call["messages"][0]["content"] == "compact summary"
    assert "original text only in backup" not in str(call["messages"])
    assert "separate memory" in call["system"]
    assert list(store.session_dir(session.id).glob("*.bak"))


# 功能：会话列表限定项目并兼容旧数据，损坏记录不影响正常结果
# 设计：混合当前项目、其他项目、旧格式、一次性和损坏会话，验证筛选及排序
async def test_list_projects_and_bind_legacy(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    manager = _manager(store, tmp_path)
    current = await manager.create("chat", "current")
    legacy = await manager.create("chat", "legacy")
    (tmp_path / "another-project").mkdir()
    foreign = await _manager(store, tmp_path / "another-project").create("chat")
    await manager.create("one_shot")
    metadata = legacy.to_dict()
    metadata.pop("workspace")
    metadata["updated_at"] = "2099-01-01T00:00:00+00:00"
    (store.session_dir(legacy.id) / "meta.json").write_text(json.dumps(metadata), encoding="utf-8")
    broken = store.session_dir("sess-broken")
    broken.mkdir()
    (broken / "meta.json").write_text("{broken", encoding="utf-8")
    manager = _manager(store, tmp_path)

    listed = manager.list_sessions()
    assert [s.id for s in listed] == [legacy.id, current.id]
    assert listed[0].workspace is None
    restored = await manager.resume(legacy.id)
    assert restored.workspace == str(tmp_path.resolve())
    assert store.read_meta(legacy.id).workspace == str(tmp_path.resolve())
    with pytest.raises(HandlerError) as exc:
        await manager.resume(foreign.id)
    assert exc.value.code == SESSION_PROJECT_MISMATCH


# 功能：非法路径、缺失及损坏会话返回结构化错误且不破坏当前会话
# 设计：参数化不同磁盘失败路径，同时检查已有历史、状态和提交回调均保持不变
@pytest.mark.parametrize("target,code", [
    ("../outside", SESSION_INVALID), ("missing", SESSION_NOT_FOUND),
    ("sess-broken", SESSION_INVALID), ("sess-mismatch", SESSION_INVALID),
])
async def test_resume_failure_keeps_current(tmp_path: Path, target: str, code: int) -> None:
    store = SessionStore(tmp_path / "sessions")
    manager = _manager(store, tmp_path)
    current = await manager.create("chat")
    store.append_message(current.id, "user", "keep me")
    for sid, body in (("sess-broken", "[]"), ("sess-mismatch", json.dumps(current.to_dict()))):
        store.session_dir(sid).mkdir()
        (store.session_dir(sid) / "meta.json").write_text(body, encoding="utf-8")
    committed = Mock()
    with pytest.raises(HandlerError) as exc:
        await manager.resume(target, current.id, on_commit=committed)
    assert exc.value.code == code
    committed.assert_not_called()
    assert store.read_meta(current.id).status == "active"
    assert store.read_messages(current.id) == [{"role": "user", "content": "keep me"}]


# 功能：关闭旧会话写盘失败时回滚目标元数据，不切换内存状态
# 设计：仅模拟第二次元数据写入失败，检测目标已写盘后的事务补偿路径
async def test_switch_write_failure_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(tmp_path)
    manager = _manager(store, tmp_path)
    current = await manager.create("chat")
    target = await manager.create("chat")
    await manager.close(target.id)
    original = store.read_meta(target.id)
    write = store.write_meta

    # 只拒绝关闭当前会话，允许恢复目标的回滚写入
    def fail_current(session):
        if session.id == current.id:
            raise OSError("disk unavailable")
        write(session)

    monkeypatch.setattr(store, "write_meta", fail_current)
    with pytest.raises(HandlerError) as exc:
        await manager.resume(target.id, current.id)
    assert exc.value.code == SESSION_INVALID
    assert store.read_meta(target.id) == original
    assert current.status == "active"


# 功能：恢复检查主运行锁和真实后台任务，同时拒绝一次性会话
# 设计：通过真实 Runner 的注册表注入存活协程，确保任务完成状态而非持久化状态决定忙碌
async def test_resume_busy_background_and_mode(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    runner = AgentRunner(KamaConfig(), provider=Mock())
    manager = SessionManager(store, lambda: runner, EventBus(), workspace=tmp_path)
    session = await manager.create("chat")
    one_shot = await manager.create("one_shot")
    with pytest.raises(HandlerError) as exc:
        await manager.resume(one_shot.id)
    assert exc.value.code == SESSION_NOT_RESUMABLE
    async with manager._locks[session.id]:
        with pytest.raises(HandlerError) as exc:
            await manager.resume(session.id)
        assert exc.value.code == SESSION_BUSY
    finished = asyncio.Event()
    task = asyncio.create_task(finished.wait())
    runner.background_tasks.register("child", task, ExecutionContext("child", "wait", 1))
    manager._background_tasks[session.id] = [runner.background_tasks]
    try:
        for operation in (manager.close(session.id), manager.resume(session.id)):
            with pytest.raises(HandlerError) as exc:
                await operation
            assert exc.value.code == SESSION_BUSY
    finally:
        finished.set()
        await task
    assert (await manager.resume(session.id)).status == "waiting_for_input"


# 功能：真实后台子 Agent 在主运行结束后仍阻止恢复，完成后可正常恢复
# 设计：模型桩实际触发 spawn_agent 并挂起子模型调用，覆盖 Runner 到 SessionManager 的注册表关联
async def test_spawned_background_agent_blocks_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    started = asyncio.Event()
    finish = asyncio.Event()

    # 主模型发起后台工具后结束，子模型等待测试明确放行
    async def chat(**kwargs):
        if kwargs["messages"][0]["content"] == "background goal":
            started.set()
            await finish.wait()
            return LlmResponse(stop_reason="end_turn", text="child done")
        if kwargs["step"] == 1:
            return LlmResponse(stop_reason="tool_use", tool_calls=[ToolCallBlock(
                id="spawn", name="spawn_agent", input={
                    "prompt": "background goal", "description": "background",
                    "run_in_background": True,
                },
            )])
        return LlmResponse(stop_reason="end_turn", text="main done")

    provider = Mock(chat=AsyncMock(side_effect=chat))
    runner = AgentRunner(KamaConfig(), provider=provider)
    manager = SessionManager(SessionStore(tmp_path / "sessions"), lambda: runner, EventBus())
    session = await manager.create("chat")
    try:
        await asyncio.wait_for(manager.send_message(session.id, "main goal"), 3)
        await asyncio.wait_for(started.wait(), 2)
        assert not manager._locks[session.id].locked()
        assert manager.is_busy(session.id)
        with pytest.raises(HandlerError) as exc:
            await manager.resume(session.id)
        assert exc.value.code == SESSION_BUSY
    finally:
        finish.set()
        await asyncio.gather(*(task for task, _ in runner.background_tasks.all()))
    assert (await manager.resume(session.id)).status == "waiting_for_input"


# 功能：恢复当前会话幂等，切换后旧会话关闭而原有历史不变
# 设计：比较完整元数据及会话目录数量，防止恢复命令隐式产生新 run 或新 session
async def test_resume_switch_and_idempotency(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = _manager(store, tmp_path)
    old = await manager.create("chat")
    target = await manager.create("chat")
    before = target.to_dict()
    assert (await manager.resume(target.id, target.id)).to_dict() == before
    restored = await manager.resume(target.id, old.id)
    assert restored.status == "waiting_for_input"
    assert store.read_meta(old.id).status == "closed"
    assert len(store.list_meta()) == 2
    assert store.read_messages(target.id) == []


# 功能：重新打开会话后旧审批失效，新工具调用必须产生新的审批请求
# 设计：让真实权限 Future 挂起后关闭再恢复，重用 tool_use_id 检查 request_id 隔离
async def test_permissions_reopen_requires_fresh_approval() -> None:
    manager = PermissionManager({"bash": ToolPolicy(PermissionDecision.ASK)}, timeout_s=0)
    events = asyncio.Queue()

    # 将真实审批请求送入队列以便准确等待请求创建
    async def emit(event):
        await events.put(event)

    # 创建同一个会话中的真实异步权限检查
    async def check():
        return await manager.check_and_wait(
            "tool", "bash", {"command": "echo hello"}, "session", emit,
        )

    first = asyncio.create_task(check())
    old = await asyncio.wait_for(events.get(), 2)
    manager.cancel_session("session")
    assert not (await first)[0]
    manager.reopen_session("session")
    assert not manager.is_closed("session")
    second = asyncio.create_task(check())
    fresh = await asyncio.wait_for(events.get(), 2)
    assert fresh["request_id"] != old["request_id"]
    assert not manager.respond("tool", "allow_once", old["request_id"])
    assert manager.respond("tool", "allow_once", fresh["request_id"])
    assert (await second)[0]


# 功能：恢复会话不会重新使用关闭前的路径授权
# 设计：实际读取工作区外文件并批准会话权限，恢复后再次读取必须产生第二次审批
async def test_reopen_does_not_restore_path_grants(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("data", encoding="utf-8")
    manager = PermissionManager({"read_file": ToolPolicy(PermissionDecision.ALLOW)})
    registry = ToolRegistry()
    registry.register(ReadFileTool(PathAccessController(workspace)))
    bus = EventBus()
    requests = []

    # 对每次真实路径请求给予会话授权并记录请求标识
    async def approve(event):
        if event.type == "permission.requested":
            requests.append(event)
            assert manager.respond(event.tool_use_id, "allow_session", event.request_id)

    bus.subscribe(approve)
    call = ToolCallBlock("read", "read_file", {"path": str(target)})
    assert not (await invoke_tool(registry, call, bus, "run", permission_manager=manager, session_id="session")).is_error
    assert manager.session_grants("session", str(workspace))
    manager.cancel_session("session")
    manager.reopen_session("session")
    assert not manager.session_grants("session", str(workspace))
    assert not (await invoke_tool(registry, call, bus, "run2", permission_manager=manager, session_id="session")).is_error
    assert len(requests) == 2


# 功能：新增命令可由协议联合解析，旧会话事件继续兼容缺少运行标识的输入
# 设计：经真实 TypeAdapter 解析命令并往返事件，检查新增协议入口和可选字段兼容性
def test_resume_protocol_compatibility() -> None:
    adapter = TypeAdapter(Command)
    assert isinstance(adapter.validate_python({"type": "session.list"}), SessionListCommand)
    assert isinstance(adapter.validate_python({
        "type": "session.resume", "session_id": "sess-1",
    }), SessionResumeCommand)
    event = SessionMessageReceivedEvent(session_id="sess-1", content="hi", ts="now")
    assert event.run_id is None
    assert SessionMessageReceivedEvent.model_validate_json(event.model_dump_json()) == event
