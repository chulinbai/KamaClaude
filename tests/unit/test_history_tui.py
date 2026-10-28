from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from rich.text import Text

from kama_claude.core.transport.socket_client import SocketClient
from kama_claude.tui.app import KamaTuiApp, PermissionSelect


# 功能：历史跨工作区审批显示完整范围并只提供本次、会话和拒绝选项
# 设计：通过真实 Textual 事件及键盘路径验证范围显示、会话绑定和最终批准参数
async def test_history_scope_approval_in_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", AsyncMock())
    app = KamaTuiApp("127.0.0.1", 9999)
    client = SocketClient("127.0.0.1", 9999)
    response = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(client, "send_command", response)
    target = "F:/projects/历史 [private]"
    async with app.run_test(size=(100, 45)) as pilot:
        app._client = client
        app._session_id = "session"
        app._handle_event({
            "type": "permission.requested", "request_id": "history-request",
            "session_id": "session", "run_id": "run", "tool_use_id": "tool",
            "tool_name": "history_search", "param_preview": "needle",
            "history_workspaces": [target, None], "paths": [], "reason": "history_scope",
        })
        await pilot.pause()
        block = app._pending_permission_blocks["history-request"]
        visible = Text.from_markup(block._pending_text()).plain
        assert target in visible and "不授予项目文件访问权" in visible
        select = app.query_one(PermissionSelect)
        assert [choice[0] for choice in select._CHOICES] == [
            "allow_once", "allow_session", "deny_once",
        ]
        await pilot.press("s")
        await pilot.pause()
        response.assert_awaited_once_with("permission.respond", {
            "tool_use_id": "tool", "decision": "allow_session",
            "request_id": "history-request", "session_id": "session",
        })
        assert not app._pending_permission_blocks
