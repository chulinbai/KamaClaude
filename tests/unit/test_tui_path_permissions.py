from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from rich.text import Text

from kama_claude.core.transport.socket_client import IpcError, SocketClient
from kama_claude.tui.app import KamaTuiApp, PermissionBlock, PermissionSelect


# 构造包含完整长路径及精确目录范围的服务端审批事件
def permission(request_id: str, session_id: str = "session") -> dict[str, Any]:
    return {
        "type": "permission.requested", "request_id": request_id, "tool_use_id": "same-tool",
        "session_id": session_id, "tool_name": "create_dir", "param_preview": "truncated...",
        "paths": [{"path": "F:\\code\\python\\" + "中文 test [folder]\\" * 8 + "test_file",
                   "kind": "directory", "access": "write", "operation": "create_dir"}],
    }


# 功能：TUI 展示完整目标路径和子树范围，路径请求不提供永久批准
# 设计：用真正 Rich 解析器检查转义后的渲染文字，避免长路径或方括号被截断和解释
def test_full_path_and_choices() -> None:
    event = permission("request")
    block = PermissionBlock("tool", "create_dir", "", event["paths"])
    visible = Text.from_markup(block._pending_text()).plain
    assert event["paths"][0]["path"] in visible
    assert "含子目录" in visible and "创建目录" in visible and "读写" in visible
    select = PermissionSelect("tool", request_id="request", path_request=True)
    assert [choice[0] for choice in select._CHOICES] == ["allow_once", "allow_session", "deny_once"]
    assert "always_allow" not in select._KEY_MAP.values()


# 功能：键盘会话批准在服务端确认后才生效，重复工具 ID 的并发审批互不影响
# 设计：真实 Textual pilot 按键，并在 RPC stub 内断言界面仍处于待审批状态
async def test_keyboard_approval_waits_for_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", AsyncMock())
    app = KamaTuiApp("127.0.0.1", 9999)
    client = SocketClient("127.0.0.1", 9999)
    calls = []

    # 模拟服务端确认前的状态，确保界面没有乐观放行
    async def acknowledge(method: str, params: dict[str, Any]) -> dict[str, bool]:
        assert method == "permission.respond"
        assert set(app._pending_permission_blocks) == {"r1", "r2"}
        assert not app._pending_permission_blocks[params["request_id"]]._resolved
        calls.append(params)
        return {"ok": True}

    monkeypatch.setattr(client, "send_command", acknowledge)
    async with app.run_test(size=(100, 45)) as pilot:
        app._client = client
        app._session_id = "session"
        app._handle_event(permission("foreign", "another"))
        assert not app._pending_permission_blocks
        app._handle_event(permission("r1"))
        app._handle_event(permission("r2"))
        await pilot.pause()
        selects = list(app.query(PermissionSelect))
        selects[-1].focus()
        await pilot.press("s")
        await pilot.pause()
        assert calls == [{"tool_use_id": "same-tool", "decision": "allow_session",
                          "request_id": "r2", "session_id": "session"}]
        assert set(app._pending_permission_blocks) == {"r1"}
        app._handle_event({"type": "permission.denied", "request_id": "r1", "decision": "timeout"})
        await pilot.pause()
        assert not list(app.query(PermissionSelect))


# 功能：服务端拒绝审批响应或关闭繁忙会话时，TUI 保留界面且不显示成功
# 设计：真实控件收到 ok=false 与 SESSION_BUSY 后检查待审批项和退出状态
async def test_rejected_ack_and_busy_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", AsyncMock())
    app = KamaTuiApp("127.0.0.1", 9999)
    client = SocketClient("127.0.0.1", 9999)

    # 分别模拟过期审批和忙碌退出，检验两条错误路径
    async def reject(method: str, params: dict[str, Any]) -> dict[str, bool]:
        if method == "session.close":
            raise IpcError(-32012, "session busy")
        return {"ok": False}

    monkeypatch.setattr(client, "send_command", reject)
    async with app.run_test(size=(100, 40)) as pilot:
        app._client = client
        app._session_id = "session"
        app._handle_event(permission("request"))
        await pilot.pause()
        await pilot.press("y")
        assert not app._pending_permission_blocks["request"]._resolved
        assert not app.query_one(PermissionSelect)._submitting
        await app.action_quit()
        assert app.is_running
        assert "request" in app._pending_permission_blocks
