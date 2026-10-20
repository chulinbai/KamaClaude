from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from textual.containers import VerticalScroll
from textual.widgets import OptionList, Static

from kama_claude.core.bus.commands import SessionSummary
from kama_claude.core.config import KamaConfig
from kama_claude.core.session.manager import SESSION_BUSY, SESSION_NOT_FOUND
from kama_claude.core.transport.socket_client import IpcError
from kama_claude.tui import __main__ as entry
from kama_claude.tui.app import ChatTextArea, KamaTuiApp
from kama_claude.tui.session_picker import SessionPicker


class _LocalApp(KamaTuiApp):
    # 保留真实 Textual 生命周期，仅关闭真实网络连接
    async def _socket_loop(self) -> None:
        await asyncio.Event().wait()


class _Client:
    # 记录真实界面发出的命令并允许注入恢复失败
    def __init__(self) -> None:
        self.calls = []
        self.errors = []

    # 模拟协议响应，明确拒绝读取历史或向模型发送恢复指令
    async def send_command(self, method, params):
        self.calls.append((method, params))
        if method == "session.create":
            return {"session_id": "sess-new", "status": "active"}
        if method == "session.list":
            return {"sessions": [
                SessionSummary(
                    session_id=sid, title=title, updated_at="2026-10-19T12:00:00Z",
                    status="closed", workspace=None, busy=busy,
                ).model_dump() for sid, title, busy in (
                    ("sess-busy", "busy", True),
                    ("sess-old", "old conversation", False),
                    ("sess-legacy", "legacy", False),
                )
            ]}
        if method == "session.resume":
            if self.errors:
                raise self.errors.pop(0)
            return {"session_id": params["session_id"], "title": "old conversation",
                    "workspace": "project", "status": "waiting_for_input"}
        raise AssertionError(f"Unexpected command: {method}")


# 功能：通过真实 TUI 键盘打开列表、取消和选择会话，成功后只显示提示
# 设计：使用 Textual Pilot 操作选项列表并断言 IPC 调用，覆盖补全入口及界面状态而非只测内部方法
async def test_resume_picker_cancel_and_select() -> None:
    app = _LocalApp("127.0.0.1", 1)
    client = _Client()
    async with app.run_test(size=(110, 36)) as pilot:
        app._client = client
        app._session_id = "sess-current"
        app._resume_target_id = "sess-current"
        app._refresh_session_input()
        app._append(Static("old visible log", classes="log-line"))
        prompt = app.query_one(ChatTextArea)
        prompt.text = "/resume "
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SessionPicker)
        options = app.screen.query_one(OptionList)
        assert options.get_option("sess-busy").disabled
        await pilot.press("escape")
        await pilot.pause()
        assert app._session_id == "sess-current"
        assert not any(method == "session.resume" for method, _ in client.calls)
        prompt.text = "/resume "
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("end", "home", "enter")
        await pilot.pause()
        assert app._session_id == "sess-old"
        assert client.calls[-1] == ("session.resume", {
            "session_id": "sess-old", "current_session_id": "sess-current",
            "workspace": str(Path.cwd()),
        })
        logs = list(app.query_one("#log-view", VerticalScroll).children)
        assert len(logs) == 1
        assert "已恢复会话" in str(logs[0].content)
        assert not prompt.disabled


# 功能：直接输入恢复指令时不发送模型消息，失败后保留当前会话和界面内容
# 设计：真实提交输入并模拟后端未找到错误，随后重试成功检查临时状态清理
async def test_resume_command_failure_then_success() -> None:
    app = _LocalApp("127.0.0.1", 1)
    client = _Client()
    client.errors.append(IpcError(SESSION_NOT_FOUND, "session not found"))
    async with app.run_test() as pilot:
        app._client = client
        app._session_id = "sess-current"
        app._resume_target_id = "sess-current"
        app._session_run_ids.add("previous-run")
        app._refresh_session_input()
        prompt = app.query_one(ChatTextArea)
        prompt.text = "/resume missing"
        await pilot.press("enter")
        await pilot.pause()
        assert app._session_id == "sess-current"
        assert app._resume_target_id == "sess-current"
        assert "previous-run" in app._session_run_ids
        assert not prompt.disabled
        prompt.text = "/resume sess-old"
        await pilot.press("enter")
        await pilot.pause()
        assert app._session_id == "sess-old"
        assert not app._session_run_ids
        assert all(method == "session.resume" for method, _ in client.calls)


# 功能：启动恢复失败仍允许再次选择，不创建替代会话
# 设计：从带恢复参数的真实应用状态开始，分别模拟后端成功和失败并检查所有命令
@pytest.mark.parametrize("fail", [False, True])
async def test_startup_resume_never_creates_session(fail: bool) -> None:
    app = _LocalApp("127.0.0.1", 1, resume_session_id="sess-old")
    client = _Client()
    if fail:
        client.errors.append(IpcError(SESSION_NOT_FOUND, "missing"))
    async with app.run_test() as pilot:
        app._client = client
        await app._open_session()
        await pilot.pause()
        assert [method for method, _ in client.calls] == ["session.resume"]
        assert app._session_id == (None if fail else "sess-old")
        assert not app.query_one(ChatTextArea).disabled


# 功能：首次创建后的重连恢复相同 ID，忙碌时重试而不新建会话
# 设计：在连接生命周期入口模拟断线，保留恢复目标并注入一次真实忙碌错误
async def test_reconnect_waits_and_preserves_session_id() -> None:
    app = _LocalApp("127.0.0.1", 1)
    client = _Client()
    async with app.run_test() as pilot:
        app._client = client
        await app._open_session()
        assert app._session_id == "sess-new"
        app._session_id = None
        client.errors.append(IpcError(SESSION_BUSY, "running"))
        await asyncio.wait_for(app._open_session(), 5)
        await pilot.pause()
        assert app._session_id == "sess-new"
        assert [method for method, _ in client.calls] == [
            "session.create", "session.resume", "session.resume",
        ]


# 功能：只接收当前会话主运行和其子运行的事件，未连接及切换后的旧事件均被忽略
# 设计：混合两个会话的关联、token 和子任务事件，检查实际呈现的文本
def test_session_event_filter_tracks_root_and_children() -> None:
    app = KamaTuiApp("127.0.0.1", 1)
    visible = []
    app._append = visible.append
    app._handle_event({"type": "llm.token", "run_id": "foreign", "token": "ignored"})
    assert not visible
    app._session_id = "sess-current"
    app._handle_event({"type": "session.message_received", "session_id": "sess-other", "run_id": "foreign"})
    app._handle_event({"type": "session.message_received", "session_id": "sess-current", "run_id": "root"})
    app._handle_event({"type": "llm.token", "run_id": "foreign", "token": "ignored"})
    app._handle_event({"type": "llm.token", "run_id": "root", "token": "visible"})
    assert len(visible) == 1
    assert visible[0]._text == "visible"
    app._handle_event({"type": "subagent.started", "run_id": "child", "parent_run_id": "root"})
    assert "child" in app._session_run_ids
    app._handle_event({"type": "subagent.started", "run_id": "foreign-child", "parent_run_id": "foreign"})
    assert "foreign-child" not in app._session_run_ids
    count = len(visible)
    app._session_id = "sess-next"
    app._session_run_ids.clear()
    app._handle_event({"type": "llm.token", "run_id": "root", "token": "late"})
    assert len(visible) == count


# 功能：恢复请求进行中仍接收当前会话的审批，避免后端拒绝切换后遗失审批界面
# 设计：模拟后台任务产生审批与恢复请求交错，检查当前会话可通过而外部会话仍被过滤
def test_switch_in_progress_keeps_current_permission_events() -> None:
    app = KamaTuiApp("127.0.0.1", 1)
    app._session_id = "sess-current"
    app._switching_session = True
    assert app._accept_session_event({"type": "permission.requested", "session_id": "sess-current"})
    assert not app._accept_session_event({"type": "permission.requested", "session_id": "sess-other"})


# 功能：启动参数传入应用且恢复与回放互斥
# 设计：执行真实 argparse 入口，只替换日志和应用运行，确保错误发生在连接之前
def test_resume_entry_argument_and_replay_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    app = Mock()
    factory = Mock(return_value=app)
    monkeypatch.setattr(entry, "get_config", KamaConfig)
    monkeypatch.setattr(entry, "_setup_logging", Mock())
    monkeypatch.setattr(entry, "KamaTuiApp", factory)
    monkeypatch.setattr("sys.argv", ["kama-tui", "--resume", "sess-old"])
    entry.main()
    assert factory.call_args.kwargs["resume_session_id"] == "sess-old"
    assert factory.call_args.kwargs["workspace"] == Path.cwd()
    app.run.assert_called_once()
    monkeypatch.setattr("sys.argv", ["kama-tui", "--resume", "sess-old", "--replay", "run-old"])
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 2
    app.run.assert_called_once()


# TUI 启动时捕获目录；之后全局 cwd 或解释器位置改变不影响创建、列表及恢复参数。
async def test_tui_keeps_launch_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "client 中文"
    core = tmp_path / "core"
    project.mkdir()
    core.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr("sys.executable", str(core / "python.exe"))
    app = _LocalApp("127.0.0.1", 1)
    monkeypatch.chdir(core)
    client = _Client()
    async with app.run_test() as pilot:
        app._client = client
        await app._open_session()
        app.push_screen_wait = AsyncMock(return_value=None)
        await app._choose_session()
        app._session_id = None
        await app._open_session()
        await pilot.pause()
        assert [method for method, _ in client.calls] == [
            "session.create", "session.list", "session.resume",
        ]
        assert all(params["workspace"] == str(project) for _, params in client.calls)
        assert Path.cwd() == core
