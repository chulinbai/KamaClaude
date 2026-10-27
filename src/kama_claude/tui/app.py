from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.markup import escape
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, Static, TextArea

from kama_claude.core.bus.commands import SessionListResult
from kama_claude.core.config import KamaConfig
from kama_claude.core.session.manager import SESSION_BUSY
from kama_claude.core.skills.loader import SkillLoader
from kama_claude.core.transport.socket_client import IpcError, SocketClient
from kama_claude.tui.agent_panel import AgentPanel
from kama_claude.tui.session_picker import SessionPicker

log = logging.getLogger(__name__)


def _preview(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s




def _params_str(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, indent=2)


# 从工具参数中提取最适合摘要展示的关键字段
def _param_summary(tool_name: str, params: dict[str, Any], max_len: int = 72) -> str:
    keys_by_tool = {
        "read_file": ("path",),
        "read_image": ("path", "image_id"),
        "write_file": ("path",),
        "list_dir": ("path", "max_depth"),
        "bash": ("command",),
        "note_save": ("content",),
    }
    keys = keys_by_tool.get(tool_name, ())
    parts = [f"{key}={params[key]!r}" for key in keys if key in params]
    if not parts:
        parts = [f"{key}={value!r}" for key, value in list(params.items())[:2]]
    return _preview(", ".join(parts), max_len)


class LLMStreamBlock(Static):
    """在同一个 Static widget 中累积 LLM 流式 token。"""

    DEFAULT_CSS = "LLMStreamBlock { padding: 0 2; color: $text; }"

    # 初始化为空文本块
    def __init__(self) -> None:
        super().__init__("")
        self._text = ""
        self._finalized = False

    # 追加一个 token 并刷新显示
    def append_token(self, token: str) -> None:
        if self._finalized:
            return
        self._text += token
        self.update(self._text)

    # 将累积文本渲染为 Markdown，供流式块结束后显示
    def finalize_markdown(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self._text.strip():
            self.update(Markdown(self._text, code_theme="monokai"))


class ToolCallBlock(Widget):
    """可折叠的工具调用块：折叠时显示摘要，点击后展开完整 params 和 output。"""

    DEFAULT_CSS = """
    ToolCallBlock { height: auto; padding: 0 2; color: $text-muted; }
    ToolCallBlock > .summary { color: $text-muted; }
    ToolCallBlock > .detail { display: none; padding: 0 2 0 4; color: $text-muted; }
    ToolCallBlock.expanded > .detail { display: block; }
    """

    # 初始化工具调用信息
    def __init__(self, tool_name: str, params: dict[str, Any]) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._params = params
        self._params_full = _params_str(params)
        self._output = ""
        self._elapsed_ms = 0
        self._is_error = False
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Static(self._summary(), classes="summary")
        yield Static("", classes="detail")

    # 生成摘要行文本
    def _summary(self) -> str:
        if self._tool_name == "note_save" and self._finished and not self._is_error:
            return f"  [green]remembered[/green]  [dim]{self._elapsed_ms}ms[/dim]"

        params_pre = _param_summary(self._tool_name, self._params)
        line = f"  [dim]tool[/dim] [bold]{self._tool_name}[/bold]"
        if params_pre:
            line += f"  [dim]{params_pre}[/dim]"
        if self._finished:
            color = "red" if self._is_error else "green"
            status = "failed" if self._is_error else "done"
            hint = "  [dim](click to expand)[/dim]" if self._output else ""
            line += f"  [{color}]{status}[/{color}]  [dim]{self._elapsed_ms}ms[/dim]{hint}"
        return line

    # 工具调用完成时更新结果并刷新摘要（widget 未挂载时跳过 DOM 更新）
    def set_result(self, output: str, elapsed_ms: int, *, is_error: bool = False) -> None:
        self._output = output
        self._elapsed_ms = elapsed_ms
        self._is_error = is_error
        self._finished = True
        if self.children:
            self.query_one(".summary", Static).update(self._summary())

    # 点击时切换展开/折叠状态
    def on_click(self) -> None:
        if not self._finished:
            return
        if "expanded" in self.classes:
            self.remove_class("expanded")
        else:
            detail = self.query_one(".detail", Static)
            detail.update(
                f"[dim]params[/dim]\n{self._params_full}\n\n"
                f"[dim]output[/dim]\n{self._output}\n\n"
                f"[dim]elapsed:[/dim] {self._elapsed_ms}ms"
            )
            self.add_class("expanded")


class PermissionSelect(Static):
    """内联权限选择控件：挂载在日志流中，键盘焦点无需 ModalScreen。"""

    can_focus = True

    DEFAULT_CSS = """
    PermissionSelect {
        height: auto;
        padding: 0 2;
        margin-bottom: 1;
    }
    """

    _CHOICES: tuple[tuple[str, str, str], ...] = (
        ("allow_once",   "Allow once",   "y / 1"),
        ("always_allow", "Always allow", "a / 2"),
        ("deny_once",    "Deny",         "n / 3"),
        ("always_deny",  "Always deny",  "d / 4"),
    )
    _KEY_MAP: dict[str, str] = {
        "y": "allow_once",  "1": "allow_once",
        "a": "always_allow","2": "always_allow",
        "n": "deny_once",   "3": "deny_once",
        "d": "always_deny", "4": "always_deny",
    }

    # 用户作出权限决策时发布，携带工具 ID 和决策字符串
    class Decided(Message):
        # 初始化决策消息，存储控件引用、工具 ID 和决策
        def __init__(self, widget: PermissionSelect, tool_use_id: str, decision: str) -> None:
            self.widget = widget
            self.tool_use_id = tool_use_id
            self.decision = decision
            super().__init__()

    # 初始化控件，存储工具 ID（用于 IPC 回复）
    def __init__(
        self, tool_use_id: str, *, request_id: str | None = None,
        path_request: bool = False, session_id: str | None = None,
    ) -> None:
        super().__init__("")
        self._tool_use_id = tool_use_id
        self.request_id = request_id
        self.session_id = session_id
        self.key = request_id or tool_use_id
        self._submitting = False
        self._cursor = 0
        if path_request:
            self._CHOICES = (
                ("allow_once", "允许本次", "y / 1"),
                ("allow_session", "允许当前会话", "s / 2"),
                ("deny_once", "拒绝", "n / 3"),
            )
            self._KEY_MAP = {
                "y": "allow_once", "1": "allow_once",
                "s": "allow_session", "2": "allow_session",
                "n": "deny_once", "3": "deny_once",
            }

    def on_mount(self) -> None:
        self.update(self._render_ui())
        self.focus()
        log.debug(
            "PermissionSelect.on_mount  can_focus=%s  focused_after=%r",
            self.can_focus,
            self.app.focused,
        )
        self.app.call_after_refresh(self._log_deferred_focus)

    # 在下一帧记录焦点是否真正转移到本控件
    def _log_deferred_focus(self) -> None:
        log.debug(
            "PermissionSelect.deferred_focus  app.focused=%r  has_focus=%s  focusable=%s",
            self.app.focused,
            self.has_focus,
            self.focusable,
        )

    # 焦点到达时记录，用于确认 focus() 是否真正生效
    def on_focus(self, event: events.Focus) -> None:
        log.debug(
            "PermissionSelect.on_focus  has_focus=%s  app.focused=%r",
            self.has_focus, self.app.focused,
        )

    # 焦点离开时记录，用于追踪是否被其他控件抢走焦点
    def on_blur(self, event: events.Blur) -> None:
        log.debug("PermissionSelect.on_blur  app.focused=%r", self.app.focused)

    # 生成带光标高亮的选项列表文本
    def _render_ui(self) -> str:
        lines: list[str] = []
        for i, (_, label, key_hint) in enumerate(self._CHOICES):
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ {label}[/bold cyan]  [dim]{key_hint}[/dim]")
            else:
                lines.append(f"    {label}  [dim]{key_hint}[/dim]")
        lines.append("[dim]  ↑↓ navigate   enter confirm[/dim]")
        return "\n".join(lines)

    # 方向键导航；快捷键直接选择；enter 确认光标位置
    def on_key(self, event: events.Key) -> None:
        log.debug("PermissionSelect.on_key  key=%r  char=%r", event.key, event.character)
        key = event.key
        if key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key == "enter":
            event.stop()
            self._pick(self._CHOICES[self._cursor][0])
        else:
            decision = self._KEY_MAP.get(key)
            if decision is not None:
                event.stop()
                self._pick(decision)

    # 发布决策消息，由宿主 App 负责 IPC 回复和控件清理
    def _pick(self, decision: str) -> None:
        log.debug("PermissionSelect._pick  decision=%s", decision)
        if not self._submitting and decision in {c[0] for c in self._CHOICES}:
            self._submitting = True
            self.post_message(self.Decided(self, self._tool_use_id, decision))


class PermissionBlock(Static):
    """日志里的权限审批摘要"""

    _LABEL_MAP: dict[str, str] = {
        "allow_once":   "allowed (once)",
        "always_allow": "always allowed",
        "allow_session": "已允许当前会话（不持久化）",
        "client_disconnected": "连接已断开，授权已撤销",
        "cancelled": "审批已取消",
        "deny_once":    "denied",
        "always_deny":  "always denied",
        "timeout":      "⏱ timed out",
    }
    LABEL_MAP = _LABEL_MAP

    # 子类提交消息：用户作出权限决策时发布
    class Resolved(Message):
        def __init__(self, block: PermissionBlock, decision: str) -> None:
            self.block = block
            self.decision = decision
            super().__init__()

    # 初始化审批块，记录工具 ID、名称和参数预览
    def __init__(
        self, tool_use_id: str, tool_name: str, param_preview: str,
        paths: list[dict[str, Any]] | None = None,
    ) -> None:
        self._tool_use_id = tool_use_id
        self._tool_name = tool_name
        self._param_preview = escape(param_preview)
        self._paths = paths or []
        self._resolved = False
        super().__init__(self._pending_text(), classes="log-line")

    # 显示完整规范化路径及作用范围，目录权限只覆盖目标及其子树
    def _path_text(self) -> str:
        lines = []
        operations = {"create_dir": "创建目录", "write": "写文件", "read": "读文件",
                      "list": "列目录", "shell": "Shell 访问"}
        for path in self._paths:
            kind = "目录（含子目录）" if path.get("kind") == "directory" else "文件（仅此文件）"
            access = "读写" if path.get("access") == "write" else "只读"
            operation = operations.get(str(path.get("operation")), "访问")
            lines.append(f"  {operation} · {access} · 授权{kind}：\n"
                         f"  [bold]{escape(str(path.get('path', '')))}[/bold]")
        if lines:
            lines.append("  [dim]仅本次或当前会话有效；不保存为永久路径权限。[/dim]")
        return "\n" + "\n".join(lines) if lines else ""

    # 生成待审批摘要并附带完整路径
    def _pending_text(self) -> str:
        preview = f"  [dim]{self._param_preview}[/dim]" if self._param_preview else ""
        return (f"[bold red]? permission[/bold red]  [bold]{escape(self._tool_name)}[/bold]"
                f"{preview}{self._path_text()}")

    # 将块收缩为单行摘要并发布 Resolved 消息
    def _resolve(self, decision: str) -> None:
        if self._resolved:
            return
        self._resolved = True
        allowed = decision in ("allow_once", "allow_session", "always_allow")
        icon = "[bold green]✓[/bold green]" if allowed else "[bold red]✗[/bold red]"
        label = self._LABEL_MAP.get(decision, decision)
        preview = f"  [dim]{self._param_preview}[/dim]" if self._param_preview else ""
        self.update(
            f"{icon} permission  [bold]{escape(self._tool_name)}[/bold]{preview}  "
            f"[dim]{label}[/dim]{self._path_text()}"
        )
        self.post_message(self.Resolved(self, decision))


class SlashCompleteWidget(Static):
    """斜杠命令自动补全弹出框：输入 / 时显示可用 skill 列表并支持键盘筛选与选择。"""

    can_focus = False

    DEFAULT_CSS = """
    SlashCompleteWidget {
        height: auto;
        padding: 0 1;
        margin: 0 2;
        background: $surface;
        border: round $surface-lighten-2;
    }
    """

    # 用户选中某条命令时发布
    class Selected(Message):
        # 初始化，携带被选中的 skill 名称
        def __init__(self, skill_name: str) -> None:
            self.skill_name = skill_name
            super().__init__()

    # 初始化，接收全量 (name, description) 列表
    def __init__(self, items: list[tuple[str, str]]) -> None:
        super().__init__("")
        self._all_items = items
        self._filtered: list[tuple[str, str]] = list(items)
        self._cursor = 0

    # 根据查询字符串筛选列表，重置光标并重新渲染
    def set_query(self, query: str) -> None:
        q = query.lower()
        self._filtered = [(n, d) for n, d in self._all_items if not q or q in n.lower()]
        self._cursor = min(self._cursor, max(0, len(self._filtered) - 1))
        if self.is_attached:
            self._redraw()

    # 向上移动光标并重新渲染
    def move_up(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor - 1) % len(self._filtered)
            self._redraw()

    # 向下移动光标并重新渲染
    def move_down(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor + 1) % len(self._filtered)
            self._redraw()

    # 选中当前光标项并发布 Selected 消息
    def select_current(self) -> None:
        if self._filtered:
            self.post_message(self.Selected(self._filtered[self._cursor][0]))

    # 返回当前是否有可选项
    def has_selection(self) -> bool:
        return len(self._filtered) > 0

    def on_mount(self) -> None:
        self._redraw()

    # 渲染筛选后的命令列表，高亮当前光标项
    def _redraw(self) -> None:
        if not self._filtered:
            self.update("[dim]  no matching commands[/dim]")
            return
        lines: list[str] = []
        for i, (name, desc) in enumerate(self._filtered):
            desc_part = f"  [dim]{desc}[/dim]" if desc else ""
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ /{name}[/bold cyan]{desc_part}")
            else:
                lines.append(f"    [cyan]/{name}[/cyan]{desc_part}")
        lines.append("[dim]  ↑↓ navigate   tab/enter select   esc dismiss[/dim]")
        self.update("\n".join(lines))


class ChatTextArea(TextArea):
    """支持 Enter 提交、Cmd/Shift/Alt+Enter 换行的多行聊天输入框。"""

    DEFAULT_CSS = """
    ChatTextArea {
        height: auto;
        min-height: 3;
        max-height: 12;
        border: round $surface-lighten-2;
        background: $background;
        padding: 0 1;
        margin: 1 2;
        scrollbar-size-vertical: 1;
    }
    ChatTextArea:focus {
        border: round $accent;
        background: $background;
    }
    """

    # 子类自定义的提交消息，供宿主 App 监听
    class Submitted(Message):
        def __init__(self, area: ChatTextArea) -> None:
            self.text_area = area
            self.value = area.text
            super().__init__()

    # 输入内容以 / 开头且无空格时发布，query 为 / 之后的字符串（可为空串）；None 表示收起弹窗
    class SlashChanged(Message):
        def __init__(self, query: str | None) -> None:
            self.query = query
            super().__init__()

    # 文本变化时检测 / 前缀，通知宿主 App 更新自动补全弹窗
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = self.text
        if text.startswith("/") and " " not in text:
            self.post_message(ChatTextArea.SlashChanged(query=text[1:]))
        else:
            self.post_message(ChatTextArea.SlashChanged(query=None))

    # Enter 提交；↑↓/Tab/Esc 路由到自动补全弹窗；Cmd/Shift/Alt+Enter 插入换行；其余键交回 TextArea
    async def _on_key(self, event: events.Key) -> None:
        key = event.key

        popup: SlashCompleteWidget | None = None
        try:
            popup = self.app.query_one(SlashCompleteWidget)
        except NoMatches:
            popup = None

        if key == "enter":
            event.stop()
            event.prevent_default()
            if popup is not None and popup.has_selection():
                popup.select_current()
                return
            if self.text.strip():
                self.post_message(self.Submitted(self))
            return
        if key in ("alt+enter", "shift+enter", "ctrl+j", "super+enter"):
            event.stop()
            event.prevent_default()
            if not self.read_only:
                self.insert("\n")
            return
        if popup is not None:
            if key == "up":
                event.stop()
                event.prevent_default()
                popup.move_up()
                return
            elif key == "down":
                event.stop()
                event.prevent_default()
                popup.move_down()
                return
            elif key == "tab":
                event.stop()
                event.prevent_default()
                popup.select_current()
                return
            elif key == "escape":
                event.stop()
                event.prevent_default()
                self.post_message(ChatTextArea.SlashChanged(query=None))
                return
        await super()._on_key(event)


class KamaTuiApp(App[None]):
    """KamaClaude TUI：终端滚屏风格，实时展示 agent 执行过程。"""

    TITLE = "KamaClaude"
    BINDINGS = [
        Binding("ctrl+q", "quit", "quit"),
        Binding("ctrl+g", "agents", "agents", priority=True),
    ]
    CSS = """
    Screen { background: $background; }
    #header {
        height: 1;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    #log-view {
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    #banner { padding: 1 2 0 2; }
    Static.user-turn { color: $text; padding: 1 2 0 2; }
    Static.run-header { color: $text-muted; padding: 1 2 0 2; }
    Static.step-divider { color: $text-muted; padding: 0 2; }
    Static.run-ok { color: green; padding: 0 2 1 2; }
    Static.run-err { color: red; padding: 0 2 1 2; }
    Static.usage { padding: 0 2; }
    Static.log-line { padding: 0 2; }
    """

    _BANNER = (
        "[bold cyan]"
        "██╗  ██╗ █████╗ ███╗   ███╗ █████╗  ██████╗██╗      █████╗ ██╗   ██╗██████╗ ███████╗"
        "[/bold cyan]\n"
        "[bold cyan]"
        "██║ ██╔╝██╔══██╗████╗ ████║██╔══██╗██╔════╝██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝"
        "[/bold cyan]\n"
        "[bold cyan]"
        "█████╔╝ ███████║██╔████╔██║███████║██║     ██║     ███████║██║   ██║██║  ██║█████╗  "
        "[/bold cyan]\n"
        "[bold cyan]"
        "██╔═██╗ ██╔══██║██║╚██╔╝██║██╔══██║██║     ██║     ██╔══██║██║   ██║██║  ██║██╔══╝  "
        "[/bold cyan]\n"
        "[bold cyan]"
        "██║  ██╗██║  ██║██║ ╚═╝ ██║██║  ██║╚██████╗███████╗██║  ██║╚██████╔╝██████╔╝███████╗"
        "[/bold cyan]\n"
        "[bold cyan]"
        "╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝ ╚═════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝"
        "[/bold cyan]\n"
        "[dim]  输入消息开始对话  ·  /resume 继续历史会话  ·  Ctrl+Q 退出[/dim]"
    )

    # 初始化连接参数和 TUI 内部状态
    def __init__(
        self, host: str, port: int, replay_run_id: str | None = None,
        resume_session_id: str | None = None,
        *, workspace: Path | None = None,
    ) -> None:
        super().__init__()
        if replay_run_id is not None and resume_session_id is not None:
            raise ValueError("resume and replay are mutually exclusive")
        self._host = host
        self._port = port
        self._workspace = workspace if workspace is not None else Path.cwd()
        self._replay_run_id = replay_run_id
        self._replaying = False
        self._resume_target_id = resume_session_id
        self._switching_session = False
        self._session_run_ids: set[str] = set()
        self._client: SocketClient | None = None
        self._current_llm: LLMStreamBlock | None = None
        self._llm_streams: dict[tuple[str, str], LLMStreamBlock] = {}
        self._pending_tool_blocks: dict[tuple[str, str], ToolCallBlock] = {}
        self._pending_permission_blocks: dict[str, PermissionBlock] = {}
        self._session_id: str | None = None
        self._busy = False
        self._last_context_pct: float = 0.0
        self._slash_items: list[tuple[str, str]] = []
        self._subagent_run_ids: dict[str, str] = {}  # child run_id -> description
        self._subagent_start_times: dict[str, float] = {}  # child run_id -> start time

    def compose(self) -> ComposeResult:
        yield Label("[bold]KamaClaude[/bold]  [dim]connecting...[/dim]", id="header")
        yield VerticalScroll(id="log-view")
        yield ChatTextArea(id="prompt", show_line_numbers=False)

    def on_mount(self) -> None:
        self._slash_items = self._build_slash_items()
        self._append(Static(self._BANNER, id="banner"))
        self.run_worker(self._socket_loop(), exclusive=True, name="socket")
        prompt = self.query_one("#prompt", ChatTextArea)
        prompt.disabled = True
        prompt.border_title = "connecting..."

    # 构建斜杠命令候选列表：内建命令 + 所有已注册 skill
    def _build_slash_items(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = [
            ("compact", "compress context window"), ("resume", "继续历史会话"),
            ("agents", "查看和管理子 agent（Ctrl+G）"),
        ]
        try:
            loader = SkillLoader(self._workspace)
            for skill in loader.list_all_skills():
                desc = skill.description.splitlines()[0] if skill.description else ""
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                if skill.name not in {"compact", "resume", "agents"}:
                    items.append((skill.name, desc))
        except Exception:
            pass
        return items

    # 即使主输入框禁用，也允许通过快捷键打开管理弹层
    def action_agents(self) -> None:
        if self._client is not None and self._session_id is not None:
            if not isinstance(self.screen, AgentPanel):
                self.push_screen(AgentPanel(self._client, self._session_id))

    # 根据 / 前缀查询字符串挂载、更新或移除自动补全弹窗
    def on_chat_text_area_slash_changed(self, event: ChatTextArea.SlashChanged) -> None:
        query = event.query
        if query is None:
            try:
                self.query_one(SlashCompleteWidget).remove()
            except NoMatches:
                pass
            return
        try:
            popup = self.query_one(SlashCompleteWidget)
            popup.set_query(query)
        except NoMatches:
            popup = SlashCompleteWidget(self._slash_items)
            self.mount(popup, before="#prompt")
            popup.set_query(query)

    # 用户选中自动补全项后将 /{name} 填入输入框并移除弹窗
    def on_slash_complete_widget_selected(self, event: SlashCompleteWidget.Selected) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.text = f"/{event.skill_name} "
            prompt.move_cursor(prompt.document.end)
        try:
            self.query_one(SlashCompleteWidget).remove()
        except NoMatches:
            pass

    # 记录按键焦点；当 PermissionSelect 失去焦点后作为兜底处理权限快捷键
    def on_key(self, event: events.Key) -> None:
        log.debug("App.on_key  key=%r  focused=%r", event.key, self.focused)
        if not self._pending_permission_blocks:
            return
        try:
            select = self.query_one(PermissionSelect)
            if select.has_focus:
                return  # PermissionSelect 有焦点时自行处理，事件不会冒泡到这里
            key = event.key
            decision = select._KEY_MAP.get(key)
            if decision:
                event.stop()
                select._pick(decision)
            elif key in ("up", "k"):
                event.stop()
                select._cursor = (select._cursor - 1) % len(select._CHOICES)
                select.update(select._render_ui())
            elif key in ("down", "j"):
                event.stop()
                select._cursor = (select._cursor + 1) % len(select._CHOICES)
                select.update(select._render_ui())
            elif key == "enter":
                event.stop()
                select._pick(select._CHOICES[select._cursor][0])
        except Exception:
            pass

    # 正常退出必须确认会话关闭，忙碌或失败时保留界面并展示原因
    async def action_quit(self) -> None:
        if self._client is not None and self._session_id is not None:
            try:
                await self._client.send_command("session.close", {"session_id": self._session_id})
            except (IpcError, RuntimeError, OSError) as exc:
                self._append(Static(
                    f"[yellow]会话尚未关闭：{escape(str(exc))}。请先处理审批或等待任务结束。[/yellow]",
                    classes="log-line",
                ))
                return
        self.exit()

    # 将输入框提交内容发送给当前 chat session；用 worker 发送，避免 await 阻塞 App 消息泵
    async def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
        content = event.value.strip()
        if not content:
            return
        if self._switching_session:
            return
        parts = content.split()
        if content == "/agents":
            event.text_area.text = ""
            self.action_agents()
            return
        if parts[0] == "/resume":
            if self._busy or self._client is None:
                self._append(Static(
                    "[yellow]任务运行中或连接尚未建立[/yellow]", classes="log-line",
                ))
                return
            if len(parts) > 2:
                self._append(Static("用法：/resume [session_id]", classes="log-line"))
                return
            event.text_area.text = ""
            operation = (
                self._restore_session(parts[1]) if len(parts) == 2 else self._choose_session()
            )
            self.run_worker(operation, name="resume", group="session", exclusive=True)
            return
        # 检测 /compact 指令
        if content == "/compact":
            event.text_area.text = ""
            if self._client is not None and self._session_id is not None and not self._busy:
                self.run_worker(self._do_compact(), name="compact", exclusive=False)
            return
        if self._client is None or self._session_id is None or self._busy:
            self._append(Static("[yellow]agent busy or disconnected[/yellow]", classes="log-line"))
            return
        self._busy = True
        prompt = event.text_area
        prompt.text = ""
        prompt.disabled = True
        prompt.read_only = False
        prompt.border_title = "agent is working..."
        self._append(Static(f"[bold]>[/bold] {content}", classes="user-turn"))
        self._update_header("running")
        self.run_worker(self._do_send_message(content), name="send_message", exclusive=False)

    # 统一更新恢复过程中的输入状态，未绑定会话时仍允许输入恢复命令
    def _refresh_session_input(self) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.disabled = self._client is None or self._switching_session or self._busy
            prompt.read_only = False
            prompt.border_title = (
                "正在恢复会话..." if self._switching_session else
                "输入 /resume 选择会话，或 Ctrl+Q 退出" if self._session_id is None else
                "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
            )
            if not prompt.disabled:
                prompt.focus()
        self._update_header("ready" if self._session_id else "select session")

    # 请求会话列表并通过现有 Textual 弹层选择，取消时保留当前状态
    async def _choose_session(self) -> None:
        client = self._client
        if client is None or self._switching_session:
            return
        selected = None
        self._switching_session = True
        self._refresh_session_input()
        try:
            result = SessionListResult.model_validate(
                await client.send_command("session.list", {"workspace": str(self._workspace)}),
            )
            if any(s.session_id == self._session_id and s.busy for s in result.sessions):
                raise IpcError(SESSION_BUSY, "当前会话仍有任务运行，请等待任务结束")
            if self._client is client:
                selected = await self.push_screen_wait(SessionPicker([
                    s for s in result.sessions if s.session_id != self._session_id
                ]))
        except (IpcError, RuntimeError, OSError, ValueError) as exc:
            self._append(Static(
                f"[red]无法读取会话列表：{escape(str(exc))}[/red]", classes="log-line",
            ))
        finally:
            self._switching_session = False
            self._refresh_session_input()
        if selected is not None and self._client is client:
            await self._restore_session(selected)

    # 恢复指定会话且只显示成功提示，重连时等待原会话的任务结束
    async def _restore_session(self, sid: str, *, retry_busy: bool = False) -> bool:
        client = self._client
        if client is None:
            return False
        if sid == self._session_id:
            return True
        self._switching_session = True
        self._refresh_session_input()
        waiting_shown = False
        try:
            while self._client is client:
                try:
                    result = await client.send_command("session.resume", {
                        "session_id": sid, "current_session_id": self._session_id,
                        "workspace": str(self._workspace),
                    })
                    break
                except IpcError as exc:
                    if not retry_busy or exc.code != SESSION_BUSY:
                        raise
                    if not waiting_shown:
                        self._append(Static(
                            "原会话仍有任务运行，正在等待恢复...", classes="log-line",
                        ))
                        waiting_shown = True
                    await asyncio.sleep(2)
            else:
                return False
            if self._client is not client:
                return False
            self._session_id = str(result["session_id"])
            self._resume_target_id = self._session_id
            self._busy = False
            self._last_context_pct = 0.0
            self._current_llm = None
            self._llm_streams.clear()
            self._pending_tool_blocks.clear()
            self._pending_permission_blocks.clear()
            self._subagent_run_ids.clear()
            self._subagent_start_times.clear()
            self._session_run_ids.clear()
            await self.query_one("#log-view", VerticalScroll).remove_children()
            for widget in self.query(PermissionSelect):
                await widget.remove()
            title = escape(str(result.get("title") or "未命名会话"))
            self._append(Static(
                f"[bold cyan]已恢复会话：{title} / {escape(self._session_id)}[/bold cyan]",
                classes="log-line",
            ))
            return True
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]恢复失败：{escape(str(exc))}[/red]", classes="log-line"))
            return False
        finally:
            self._switching_session = False
            if self._client is client:
                self._refresh_session_input()

    # 首次连接创建会话，指定恢复目标或断线重连时始终复用原会话
    async def _open_session(self) -> None:
        if self._client is None:
            return
        if self._resume_target_id is not None:
            await self._restore_session(self._resume_target_id, retry_busy=True)
            return
        created = await self._client.send_command("session.create", {
            "mode": "chat", "workspace": str(self._workspace),
        })
        self._session_id = str(created["session_id"])
        self._resume_target_id = self._session_id
        self._refresh_session_input()

    # 在 worker 中执行手动压缩命令，完成后显示结果横幅
    async def _do_compact(self) -> None:
        if self._client is None or self._session_id is None:
            return
        self._append(Static("[dim]⚡ compacting context...[/dim]", classes="log-line"))
        try:
            result = await self._client.send_command(
                "session.compact",
                {"session_id": self._session_id, "focus": ""},
            )
            summary_tokens = result.get("summary_tokens", 0)
            saved_tokens = result.get("saved_tokens", 0)
            self._last_context_pct = 0.0
            self._append(Static(
                f"[bold cyan]⚡ Context compacted[/bold cyan]"
                f"  [dim]summary={summary_tokens} tokens  saved≈{saved_tokens} tokens[/dim]",
                classes="log-line",
            ))
        except (IpcError, RuntimeError, OSError) as e:
            self._append(Static(f"[red]compact error: {e}[/red]", classes="log-line"))

    # 在 worker 中执行 IPC 发送，使 App 消息泵在 agent 运行期间仍能处理键盘/焦点等消息
    async def _do_send_message(self, content: str) -> None:
        if self._client is None:
            return
        try:
            await self._client.send_command(
                "session.send_message",
                {"session_id": self._session_id, "content": content},
            )
        except (IpcError, RuntimeError, OSError) as e:
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
            self._update_header("ready")
            self._append(Static(f"[red]send error: {e}[/red]", classes="log-line"))

    # 收到服务端确认后才更新审批状态；失败时保留控件供重试
    async def on_permission_select_decided(self, msg: PermissionSelect.Decided) -> None:
        try:
            if self._client is None:
                raise RuntimeError("连接已断开")
            result = await self._client.send_command("permission.respond", {
                "tool_use_id": msg.tool_use_id, "decision": msg.decision,
                "request_id": msg.widget.request_id, "session_id": msg.widget.session_id,
            })
            if not result.get("ok", False):
                raise RuntimeError("审批未被接受：请求已过期、已处理或不属于当前会话")
            self._finish_permission(msg.widget.key, msg.decision)
        except (IpcError, RuntimeError, OSError) as exc:
            msg.widget._submitting = False
            self._append(Static(f"[red]审批响应失败：{escape(str(exc))}[/red]", classes="log-line"))

    # 按请求标识清理对应控件，避免并发审批互相覆盖
    def _finish_permission(self, key: str, decision: str) -> None:
        block = self._pending_permission_blocks.pop(key, None)
        if block is not None:
            block._resolve(decision)
        for select in self.query(PermissionSelect):
            if select.key == key:
                select.remove()
        if not self._pending_permission_blocks:
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                prompt.focus()

    # 向日志视图追加一个 widget 并滚动到底部
    def _append(self, widget: Widget) -> None:
        log_view = self.query_one("#log-view", VerticalScroll)
        log_view.mount(widget)
        log_view.scroll_end(animate=False)

    # 结束当前 LLM 流式块（下一个 token 将开启新块）
    def _break_llm(self, run_id: str | None = None) -> None:
        for key in list(self._llm_streams):
            if run_id is None or key[1] == run_id:
                block = self._llm_streams.pop(key)
                block.finalize_markdown()
                if self._current_llm is block:
                    self._current_llm = None

    # 将选择控件挂载到 Screen 顶层（#prompt 之前），避免 VerticalScroll 争抢焦点
    def _mount_permission_select(self, select: PermissionSelect) -> None:
        self.mount(select, before="#prompt")

    # 安全获取输入框，便于组件测试中未挂载时跳过 UI 操作
    def _prompt(self) -> ChatTextArea | None:
        try:
            return self.query_one("#prompt", ChatTextArea)
        except Exception:
            return None

    # 生成 context 占用率的彩色进度条字符串
    def _render_ctx_bar(self, pct: float) -> str:
        filled = int(pct * 20)
        bar = "█" * filled + "░" * (20 - filled)
        label = f"ctx:{pct * 100:.1f}%"
        if pct >= 0.85:
            color = "bold red"
        elif pct >= 0.70:
            color = "yellow"
        else:
            color = "dim"
        return f"[{color}]{label} {bar}[/{color}]"

    # 根据连接和运行状态刷新顶部标题
    def _update_header(self, state: str) -> None:
        try:
            header = self.query_one("#header", Label)
        except NoMatches:
            return
        session = f"  [dim]{self._session_id}[/dim]" if self._session_id else ""
        color = {
            "ready": "green",
            "running": "yellow",
            "disconnected": "red",
            "connecting": "dim",
        }.get(state, "dim")
        header.update(
            f"[bold]KamaClaude[/bold]  [dim]{self._host}:{self._port}[/dim]"
            f"{session}  [{color}]{state}[/{color}]"
        )

    # 管理 SocketClient 生命周期：连接、订阅事件、断线重连
    async def _socket_loop(self) -> None:
        header = self.query_one("#header", Label)

        while True:
            client = SocketClient(self._host, self._port)
            self._client = None
            try:
                await client.connect()
            except (ConnectionRefusedError, OSError):
                log.warning("connection refused %s:%s, retrying", self._host, self._port)
                self._update_header("disconnected")
                await asyncio.sleep(2)
                continue

            log.info("connected to %s:%s", self._host, self._port)
            self._client = client
            self._update_header("connecting")
            loop_task = asyncio.create_task(client.run_event_loop())

            async def on_event(event: dict[str, Any]) -> None:
                self._handle_event(event)

            client.on_event(on_event)

            try:
                loop_task.add_done_callback(
                    lambda t: log.error("loop_task failed: %s", t.exception())
                    if not t.cancelled() and t.exception() is not None
                    else None
                )
                params: dict[str, Any] = {
                    "topics": [
                        "session.*",
                        "run.*",
                        "step.*",
                        "tool.*",
                        "llm.token",
                        "llm.usage",
                        "log.*",
                        "permission.*",
                        "context.*",
                        "subagent.*",
                        "skill.*",
                    ],
                    "scope": "global",
                }
                if self._replay_run_id is not None:
                    params["replay_from_run"] = self._replay_run_id
                    self._replaying = True
                await client.send_command("event.subscribe", params)
                self._replaying = False
                self._replay_run_id = None
                await self._open_session()
                await loop_task
            except (IpcError, RuntimeError, OSError) as e:
                header.update(f"[bold]KamaClaude[/bold]  [red]subscribe error: {e}[/red]")
            finally:
                self._replaying = False
                if not loop_task.done():
                    loop_task.cancel()
                await asyncio.gather(loop_task, return_exceptions=True)
                if isinstance(self.screen, SessionPicker):
                    self.screen.dismiss(None)
                for key in list(self._pending_permission_blocks):
                    self._finish_permission(key, "client_disconnected")
                self._client = None
                self._session_id = None
                self._busy = False
                self._session_run_ids.clear()
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = True
                    prompt.read_only = False
                    prompt.border_title = "disconnected, retrying..."
                self._break_llm()
                await client.close()

            self._update_header("disconnected")
            await asyncio.sleep(2)

    # 根据事件 type 路由到对应渲染逻辑；捕获异常防止 socket loop 因单个事件崩溃
    def _handle_event(self, event: dict[str, Any]) -> None:
        try:
            if self._accept_session_event(event):
                self._handle_event_inner(event)
        except Exception:
            log.exception("_handle_event crashed  event_type=%s", event.get("type", "?"))

    # 用会话消息事件关联主运行和子运行，过滤其他窗口或切换前的事件
    def _accept_session_event(self, event: dict[str, Any]) -> bool:
        if self._replaying:
            return True
        if self._session_id is None:
            return False
        event_type = event.get("type", "")
        sid = event.get("session_id")
        if sid is not None:
            if sid != self._session_id:
                return False
            if event_type == "session.message_received" and event.get("run_id"):
                self._session_run_ids.add(str(event["run_id"]))
            return True
        run_id = event.get("run_id")
        if event_type == "subagent.started" and event.get("parent_run_id") in self._session_run_ids:
            self._session_run_ids.add(str(run_id))
            return True
        if event_type in {"permission.granted", "permission.denied"}:
            key = str(event.get("request_id") or event.get("tool_use_id", ""))
            if key in self._pending_permission_blocks:
                return True
        return run_id is None or run_id in self._session_run_ids

    # 实际的事件路由逻辑
    def _handle_event_inner(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "permission.requested" and event.get("session_id") != self._session_id:
            return
        if t.startswith("session."):
            if self._session_id and event.get("session_id") != self._session_id:
                return
            if self._switching_session and t in {"session.waiting_for_input", "session.closed"}:
                return

        if t == "llm.token":
            token = event.get("token", "")
            stream_key = (str(event.get("agent_id") or "main"), str(event.get("run_id", "")))
            if stream_key not in self._llm_streams:
                llm_block = LLMStreamBlock()
                if stream_key[0] != "main":
                    self._append(Static(
                        f"[dim]agent {escape(stream_key[0])}[/dim]", classes="log-line",
                    ))
                self._append(llm_block)
                self._llm_streams[stream_key] = llm_block
            self._current_llm = self._llm_streams[stream_key]
            self._current_llm.append_token(token)
            return

        if event.get("run_id"):
            self._break_llm(str(event["run_id"]))

        if t == "session.waiting_for_input":
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                prompt.focus()
            self._update_header("ready")

        elif t == "session.closed":
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.read_only = False
                prompt.border_title = "session closed"
            self._update_header("disconnected")

        elif t == "run.started":
            run_id = event.get("run_id", "")
            goal = event.get("goal", "")
            self._append(Static(
                f"[dim]run[/dim]  [cyan]{run_id}[/cyan]  [dim]{_preview(goal, 96)}[/dim]",
                classes="run-header",
            ))

        elif t == "skill.invoked":
            skill_name = event.get("skill_name", "")
            arguments = event.get("arguments", "")
            args_preview = _preview(arguments, 80) if arguments else ""
            args_part = f"  [dim]{args_preview}[/dim]" if args_preview else ""
            self._append(Static(
                f"[bold cyan]/{skill_name}[/bold cyan]{args_part}",
                classes="log-line",
            ))

        elif t == "subagent.started":
            run_id = event.get("run_id", "")
            description = event.get("description", "")
            self._subagent_run_ids[run_id] = description
            self._subagent_start_times[run_id] = time.monotonic()
            short_id = run_id[:8] if len(run_id) >= 8 else run_id
            self._append(Static(
                f"[dim]┌─[/dim] [cyan]{_preview(description, 72)}[/cyan]  [dim]{short_id}[/dim]",
                classes="log-line",
            ))

        elif t == "subagent.finished":
            run_id = event.get("run_id", "")
            status = event.get("status", "")
            description = self._subagent_run_ids.pop(run_id, event.get("description", ""))
            start = self._subagent_start_times.pop(run_id, None)
            elapsed = f"  [dim]{time.monotonic() - start:.1f}s[/dim]" if start is not None else ""
            desc_part = f"[cyan]{_preview(description, 72)}[/cyan]{elapsed}"
            if status == "success":
                self._append(Static(
                    f"[dim]└─[/dim] [bold green]✓[/bold green] {desc_part}",
                    classes="log-line",
                ))
            else:
                self._append(Static(
                    f"[dim]└─[/dim] [bold red]✗[/bold red] {desc_part}",
                    classes="log-line",
                ))

        elif t == "step.started":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            step = event.get("step", "")
            self._append(Static(
                f"[dim]step {step}[/dim]",
                classes="step-divider",
            ))

        elif t == "tool.call_started":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            params = event.get("params") or {}
            run_id = event.get("run_id", "")
            tc_block = ToolCallBlock(tool_name, params)
            if run_id in self._subagent_run_ids:
                tc_block.styles.padding = (0, 2, 0, 6)
            self._pending_tool_blocks[(str(run_id), tool_use_id)] = tc_block
            self._append(tc_block)

        elif t == "tool.call_finished":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_key = (str(event.get("run_id", "")), tool_use_id)
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            output = str(event.get("output") or "")
            if tool_key in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_key)
                tc_done.set_result(output, elapsed_ms)

        elif t == "tool.call_failed":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_key = (str(event.get("run_id", "")), tool_use_id)
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            error_msg = str(event.get("error_message") or "")
            if tool_key in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_key)
                tc_done.set_result(error_msg, elapsed_ms, is_error=True)

        elif t == "run.finished":
            if event.get("agent_id") not in (None, "main"):
                return
            status = event.get("status", "")
            steps = event.get("steps", 0)
            reason = event.get("reason") or ""
            if status == "success":
                self._append(Static(
                    f"[bold green]✓ completed[/bold green]  [dim]{steps} steps[/dim]",
                    classes="run-ok",
                ))
            else:
                detail = f"  [dim]{reason}[/dim]" if reason else ""
                self._append(Static(
                    f"[bold red]✗ failed[/bold red]{detail}  [dim]{steps} steps[/dim]",
                    classes="run-err",
                ))

        elif t == "llm.usage":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            pct = float(event.get("context_pct") or 0.0)
            self._last_context_pct = pct
            ctx_bar = self._render_ctx_bar(pct)
            self._append(Static(
                f"[dim]  tokens  "
                f"in={event.get('input_tokens')} "
                f"out={event.get('output_tokens')} "
                f"cache={event.get('cache_read_input_tokens')}[/dim]"
                f"  {ctx_bar}",
                classes="usage",
            ))

        elif t == "context.compacted":
            orig = event.get("original_tokens", 0)
            summary = event.get("summary_tokens", 0)
            self._last_context_pct = 0.0
            self._append(Static(
                f"[bold cyan]⚡ Context compacted[/bold cyan]"
                f"  [dim]original≈{orig} tokens → summary={summary} tokens[/dim]",
                classes="log-line",
            ))

        elif t == "permission.requested":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            try:
                _focused_repr = repr(self.focused)
            except Exception:
                _focused_repr = "?"
            log.info(
                "permission.requested tool=%s id=%s  app.focused=%s",
                tool_name, tool_use_id, _focused_repr,
            )
            request_id = event.get("request_id")
            paths = event.get("paths", [])
            key = str(request_id or tool_use_id)
            if key in self._pending_permission_blocks:
                return
            if paths:
                param_preview = str(event.get("params", {}).get("command", param_preview))
            perm_block = PermissionBlock(tool_use_id, tool_name, param_preview, paths)
            self._pending_permission_blocks[key] = perm_block
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.border_title = "permission required"
            self._append(perm_block)
            select = PermissionSelect(
                tool_use_id, request_id=request_id, path_request=bool(paths),
                session_id=event.get("session_id"),
            )
            self._mount_permission_select(select)
            log.debug(
                "PermissionSelect mounted before #prompt  pending=%d",
                len(self._pending_permission_blocks),
            )

        elif t in ("permission.denied", "permission.granted"):
            key = str(event.get("request_id") or event.get("tool_use_id", ""))
            if key in self._pending_permission_blocks:
                self._finish_permission(key, str(event.get("decision", "denied")))

        elif t == "log.line":
            level = event.get("level", "INFO")
            color = "bold red" if level == "ERROR" else ("yellow" if level == "WARNING" else "dim")
            self._append(Static(
                f"[{color}]{level}[/{color}]  "
                f"[dim]{event.get('source', '')}[/dim]  {event.get('message', '')}",
                classes="log-line",
            ))


# TUI 入口：读取配置并启动 KamaTuiApp
def run(config: KamaConfig, replay_run_id: str | None = None) -> None:
    app = KamaTuiApp(config.host, config.port, replay_run_id=replay_run_id)
    app.run()
