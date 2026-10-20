from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from kama_claude.core.transport.socket_client import IpcError, SocketClient


class AgentPanel(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss_panel", "返回")]
    DEFAULT_CSS = """
    AgentPanel { align: center middle; }
    AgentPanel > Vertical {
        width: 90%; height: 90%; border: round $accent;
        background: $surface; padding: 1 2;
    }
    AgentPanel OptionList { height: 1fr; min-height: 5; }
    AgentPanel #agent-result { height: 1fr; overflow-y: auto; margin: 1 0; }
    AgentPanel Horizontal { height: auto; }
    AgentPanel Button { margin-right: 1; min-width: 12; }
    AgentPanel #agent-notice { height: auto; }
    """

    # 管理弹层独立于主输入框，因此主任务运行时仍可使用
    def __init__(self, client: SocketClient, session_id: str) -> None:
        super().__init__()
        self._client = client
        self._session_id = session_id
        self._agents: dict[str, dict[str, Any]] = {}
        self.selected_id: str | None = None
        self._refreshing = False
        self._operating = False

    # 复用现有 Textual 列表、文本和按钮组件
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Agent 管理 · ↑↓ 选择 · Esc 返回")
            yield OptionList(id="agent-options")
            yield Static("请选择 agent", id="agent-result", markup=False)
            yield Input(placeholder="给选中 agent 的消息或追加任务", id="agent-message")
            with Horizontal():
                yield Button("发送消息", id="agent-send")
                yield Button("追加任务", id="agent-wake", variant="primary")
                yield Button("关闭 Agent", id="agent-close", variant="error")
                yield Button("刷新", id="agent-refresh")
            yield Label("", id="agent-notice", markup=False)

    # 打开后读取状态并定期刷新，关闭弹层后定时器由 Textual 回收
    def on_mount(self) -> None:
        self.call_after_refresh(self.refresh_agents)
        self.set_interval(1.0, self.refresh_agents)
        self.query_one(OptionList).focus()

    # 获取最新列表但保留选中身份，不移动正在输入的焦点
    async def refresh_agents(self) -> None:
        if not self.is_mounted or self._refreshing or self._operating:
            return
        self._refreshing = True
        try:
            result = await self._client.send_command(
                "subagent.list",
                {"session_id": self._session_id},
            )
            if not self.is_mounted:
                return
            self._agents = {a["agent_id"]: a for a in result["agents"]}
            selected = self.selected_id
            options = self.query_one(OptionList)
            options.clear_options()
            for agent_id, agent in self._agents.items():
                options.add_option(
                    Option(
                        Text(
                            f"{agent['description']}  [{agent['state']}]  "
                            f"待处理消息 {agent['pending_messages']}\n{agent_id}",
                        ),
                        id=agent_id,
                    )
                )
            keys = list(self._agents)
            if keys:
                options.highlighted = keys.index(selected) if selected in keys else 0
                self.selected_id = keys[options.highlighted]
            self._show_selected()
        except (IpcError, OSError, RuntimeError) as exc:
            if self.is_mounted:
                self.query_one("#agent-notice", Label).update(str(exc))
        finally:
            self._refreshing = False

    # 显示所选 agent 的最近结果与可执行操作
    def _show_selected(self) -> None:
        agent = self._agents.get(self.selected_id or "")
        if agent is None:
            return
        result = agent.get("result") or {}
        text = (
            f"{agent['description']} · {agent['state']}\n"
            f"当前执行：{agent.get('run_id') or '空闲'}\n"
            f"待处理消息：{agent['pending_messages']}\n"
            f"最近结果：{result.get('status', '暂无')}\n"
            f"{result.get('result') or result.get('reason') or ''}"
        )
        if agent.get("discarded_messages"):
            text += f"\n关闭时丢弃未处理消息：{agent['discarded_messages']} 条"
        self.query_one("#agent-result", Static).update(text)
        closed = agent["state"] in {"CLOSING", "CLOSED"}
        for button_id in ("agent-send", "agent-wake", "agent-close"):
            self.query_one(f"#{button_id}", Button).disabled = (
                self._operating
                or closed
                or (agent["agent_id"] == "main" and button_id != "agent-send")
            )

    # 列表移动时切换结果视图
    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        self.selected_id = event.option_id
        self._show_selected()

    # 管理操作使用专用 IPC，不经过主会话的普通用户输入入口
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "agent-refresh":
            await self.refresh_agents()
            return
        if self._operating or self.selected_id is None:
            return
        self._operating = True
        self._show_selected()
        try:
            params: dict[str, Any] = {
                "session_id": self._session_id,
                "agent_id": self.selected_id,
            }
            if event.button.id == "agent-close":
                await self._client.send_command("subagent.close", params)
                notice = "Agent 及其后代已关闭"
            else:
                message = self.query_one("#agent-message", Input).value
                params.update(message=message, wake=event.button.id == "agent-wake")
                await self._client.send_command("subagent.send_input", params)
                if not self.is_mounted:
                    return
                self.query_one("#agent-message", Input).value = ""
                notice = "消息已接受；处理情况请查看 agent 状态"
            if self.is_mounted:
                self.query_one("#agent-notice", Label).update(notice)
        except (IpcError, OSError, RuntimeError) as exc:
            if self.is_mounted:
                self.query_one("#agent-notice", Label).update(str(exc))
        finally:
            self._operating = False
            await self.refresh_agents()

    # 返回主界面，不改变任何 agent 的执行状态
    def action_dismiss_panel(self) -> None:
        self.dismiss(None)
