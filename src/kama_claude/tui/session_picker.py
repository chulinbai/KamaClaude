from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

from kama_claude.core.bus.commands import SessionSummary


class SessionPicker(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "取消")]
    DEFAULT_CSS = """
    SessionPicker { align: center middle; }
    SessionPicker > Vertical {
        width: 85%; height: 80%; border: round $accent;
        background: $surface; padding: 1 2;
    }
    SessionPicker OptionList { height: 1fr; }
    SessionPicker Label { height: auto; margin-bottom: 1; }
    """

    # 保存服务端排序后的会话摘要，当前会话由调用方排除
    def __init__(self, sessions: list[SessionSummary]) -> None:
        super().__init__()
        self._sessions = sessions

    # 使用现有选项列表显示标题、项目和占用状态
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("继续历史会话 · ↑↓ 选择 · Enter 打开 · Esc 取消")
            options = []
            for session in self._sessions:
                state = "其他窗口已连接" if session.attached_elsewhere else (
                    "任务运行中" if session.busy else "可恢复"
                )
                workspace = session.workspace or "项目未知（恢复后绑定当前项目）"
                options.append(Option(Text(
                    f"{session.title or '未命名会话'}  [{state}]\n"
                    f"{session.session_id} · {session.updated_at}\n{workspace}",
                ), id=session.session_id, disabled=session.busy or session.attached_elsewhere))
            if not options:
                yield Label("当前项目暂无可选择的历史会话。")
            yield OptionList(*options, id="session-options")

    # 挂载后将键盘焦点交给会话列表
    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    # 返回选中的完整会话标识并关闭选择界面
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option_id)

    # 取消选择且不修改当前会话
    def action_cancel(self) -> None:
        self.dismiss(None)
