from __future__ import annotations

import asyncio
from typing import Any

from textual.widget import Widget
from textual.widgets import Input, OptionList

from kama_claude.tui.agent_panel import AgentPanel
from kama_claude.tui.app import ChatTextArea, KamaTuiApp, LLMStreamBlock, ToolCallBlock


class _LocalApp(KamaTuiApp):
    # 保留真实 Textual 生命周期，仅关闭网络连接
    async def _socket_loop(self) -> None:
        await asyncio.Event().wait()


class _Client:
    # 记录管理命令并返回可交互的空闲子身份
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.child = {
            "agent_id": "child",
            "description": "worker",
            "state": "IDLE",
            "pending_messages": 0,
            "run_id": None,
            "result": {"status": "success", "result": "previous answer"},
        }

    # 模拟专用 IPC 的结果，拒绝意外经过主用户输入入口
    async def send_command(self, method, params):
        self.calls.append((method, params))
        if method == "subagent.list":
            return {
                "agents": [
                    {
                        "agent_id": "main",
                        "description": "main",
                        "state": "RUNNING",
                        "pending_messages": 0,
                        "run_id": "root-run",
                    },
                    dict(self.child),
                ]
            }
        if method == "subagent.send_input":
            self.child["pending_messages"] += 1
            return {"data": {"accepted": True}}
        if method == "subagent.close":
            self.child["state"] = "CLOSED"
            return {"data": dict(self.child)}
        raise AssertionError(method)


# 功能：主输入禁用期间 Ctrl+G 仍能管理子线程，发送和追加任务使用不同 wake 语义
# 设计：通过真实 Pilot 键盘和按钮操作弹层，验证专用 IPC 参数及返回主界面后的输入状态
async def test_agent_panel_while_main_busy() -> None:
    app = _LocalApp("127.0.0.1", 1)
    client = _Client()
    async with app.run_test(size=(120, 40)) as pilot:
        app._client = client
        app._session_id = "s"
        app._busy = True
        app._refresh_session_input()
        prompt = app.query_one(ChatTextArea)
        assert prompt.disabled
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert isinstance(app.screen, AgentPanel)
        panel = app.screen
        panel.query_one(OptionList).highlighted = 1
        await pilot.pause()
        panel.query_one(Input).value = "message"
        await pilot.click("#agent-send")
        await pilot.pause()
        assert (
            "subagent.send_input",
            {
                "session_id": "s",
                "agent_id": "child",
                "message": "message",
                "wake": False,
            },
        ) in client.calls
        panel.query_one(Input).value = "next task"
        await pilot.click("#agent-wake")
        await pilot.pause()
        assert (
            "subagent.send_input",
            {
                "session_id": "s",
                "agent_id": "child",
                "message": "next task",
                "wake": True,
            },
        ) in client.calls
        await pilot.click("#agent-close")
        await pilot.pause()
        assert client.child["state"] == "CLOSED"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, AgentPanel)
        assert app._busy and prompt.disabled


# 功能：交错文本及相同工具调用 ID 不会串流，子任务结束不能解除主输入忙碌状态
# 设计：两个运行交替发送 token 与工具事件，检查各自累积文本和结果归属
def test_interleaved_streams_and_duplicate_tool_ids() -> None:
    app = KamaTuiApp("127.0.0.1", 1)
    app._session_id = "s"
    app._busy = True
    appended: list[Widget] = []
    app._append = lambda widget: appended.append(widget)

    # 统一补充当前会话和运行身份
    def emit(agent: str, event_type: str, **fields: Any) -> None:
        app._handle_event(
            {
                "type": event_type,
                "session_id": "s",
                "agent_id": agent,
                "run_id": f"run-{agent}",
                "ts": "t",
                **fields,
            }
        )

    emit("a", "llm.token", token="A1")
    emit("b", "llm.token", token="B1")
    emit("a", "llm.token", token="A2")
    emit("b", "tool.call_started", tool_use_id="same", tool_name="read_file", params={})
    emit("a", "llm.token", token="A3")
    emit("a", "tool.call_started", tool_use_id="same", tool_name="read_file", params={})
    emit("b", "tool.call_finished", tool_use_id="same", output="B result", elapsed_ms=1)
    emit("a", "tool.call_finished", tool_use_id="same", output="A result", elapsed_ms=1)
    streams = [widget for widget in appended if isinstance(widget, LLMStreamBlock)]
    tools = [widget for widget in appended if isinstance(widget, ToolCallBlock)]
    assert [widget._text for widget in streams] == ["A1A2A3", "B1"]
    assert [widget._output for widget in tools] == ["B result", "A result"]
    emit("a", "run.finished", status="success", steps=1)
    assert app._busy
    assert not app._pending_tool_blocks
