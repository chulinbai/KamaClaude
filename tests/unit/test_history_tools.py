from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.bus.events import (
    PermissionDeniedEvent,
    PermissionGrantedEvent,
    PermissionRequestedEvent,
)
from kama_claude.core.events.bus import EventBus
from kama_claude.core.history.models import HistoryConfig, record_text
from kama_claude.core.history.service import HistoryService
from kama_claude.core.history.tools import HistorySearchTool
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore


# 功能：通用工具始终允许不能绕过历史范围审批，历史授权不扩张路径权限
# 设计：预置工具级 allow，仍必须审批具体目标且随后检查路径授权集合为空
async def test_always_allow_cannot_bypass_history_scope(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.write_meta(Session(
        id="sess-other", mode="chat", status="closed", title="other", created_at="a",
        updated_at="b", workspace=str(tmp_path / "other"),
    ))
    store.history("sess-other").emit("user_message", {"content": "needle"})
    service = HistoryService(store)
    manager = PermissionManager()
    manager._persistent_always["history_search"] = "allow"
    bus = EventBus()
    events: list[Any] = []

    # 捕获范围请求并授予单次历史读取
    async def approve(event: Any) -> None:
        events.append(event)
        if isinstance(event, PermissionRequestedEvent):
            manager.respond(event.tool_use_id, "allow_once", event.request_id,
                            session_id=event.session_id)

    bus.subscribe(approve)
    tool = HistorySearchTool(service, tmp_path, "caller", "run", bus, manager)
    try:
        await tool.prepare({"query": "needle", "scope": "all"}, "tool-id")
        result = await tool.invoke({"query": "needle", "scope": "all"})
        assert not result.is_error and "needle" in result.content
        assert any(isinstance(event, PermissionRequestedEvent) for event in events)
        assert any(isinstance(event, PermissionGrantedEvent) for event in events)
        assert manager.session_grants("caller", str(tmp_path)) == ()
        assert not manager.history_grants("caller", str(tmp_path))
        manager.cancel_session("caller")
        assert (await tool.invoke({"query": "needle", "scope": "all"})).is_error
    finally:
        await service.close()


# 功能：历史工具输出和模型思考不进入检索投影，原始归档仍保存完整数据
# 设计：直接检查投影与 payload 两种视图，避免靠删除历史内容实现排除
def test_search_projection_excludes_recursive_evidence(tmp_path: Path) -> None:
    archive = SessionStore(tmp_path / "sessions").history("sess-one")
    archive.emit("tool_result", {"content": "private recalled evidence"},
                 source={"tool_name": "history_read"})
    archive.emit("assistant_message", {"content": [
        {"type": "thinking", "thinking": "private thought"},
        {"type": "text", "text": "visible answer"},
    ]})
    records = [record for record, _ in archive.records(archive.snapshot(), [])]
    assert record_text(records[0]) == ""
    assert records[0].payload["content"] == "private recalled evidence"
    assert record_text(records[1]) == "visible answer"


# 功能：配置拒绝非法后端、非有限预算和超范围结果数
# 设计：通过实际配置模型验证公开配置边界，而不镜像服务内部实现
@pytest.mark.parametrize("values", [
    {"backend": "unknown"}, {"segment_max_bytes": 0}, {"max_results": 51},
    {"search_timeout_seconds": float("nan")}, {"segment_max_bytes": True},
])
def test_history_config_validation(values: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        HistoryConfig.model_validate(values)


# 功能：取消待审批的历史工具时发出对应结束事件并撤销待办请求
# 设计：停在真实审批 Future 上取消任务，检查 UI 依赖的 request_id 与取消决策一致
async def test_cancelled_scope_approval_emits_resolution(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.write_meta(Session(
        id="sess-other", mode="chat", status="closed", title="other", created_at="a",
        updated_at="b", workspace=str(tmp_path / "other"),
    ))
    service = HistoryService(store)
    manager = PermissionManager(timeout_s=0)
    bus = EventBus()
    events: list[Any] = []
    requested = asyncio.Event()

    # 记录审批请求但不作答，让测试明确控制取消时点
    async def receive(event: Any) -> None:
        events.append(event)
        if isinstance(event, PermissionRequestedEvent):
            requested.set()

    bus.subscribe(receive)
    tool = HistorySearchTool(service, tmp_path, "caller", "run", bus, manager)
    task = asyncio.create_task(tool.prepare({"query": "needle", "scope": "all"}, "tool-id"))
    try:
        await asyncio.wait_for(requested.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert isinstance(events[-1], PermissionDeniedEvent)
        assert events[-1].decision == "cancelled"
        assert events[-1].request_id == events[0].request_id
        assert manager.request_session(events[0].request_id) is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()
