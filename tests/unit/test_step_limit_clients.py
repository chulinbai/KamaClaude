from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from kama_claude.cli import main as cli
from kama_claude.cli.commands import chat, run
from kama_claude.core.config import KamaConfig
from kama_claude.tui import __main__ as tui
from kama_claude.tui.app import KamaTuiApp


# 功能：三个命令行入口均校验上限，恢复会话也能接收当前启动参数
# 设计：只替换实际运行与日志，保留 argparse 的解析及错误退出行为
@pytest.mark.parametrize("entry,args", [
    (cli, ["kama", "run", "--goal", "test"]), (cli, ["kama", "chat"]),
    (tui, ["kama-tui", "--resume", "saved-session"]),
])
def test_entry_limits(entry, args: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entry, "get_config", KamaConfig)
    action = Mock()
    if entry is tui:
        monkeypatch.setattr(entry, "_setup_logging", Mock())
        monkeypatch.setattr(entry, "KamaTuiApp", action)
    else:
        monkeypatch.setattr(entry, "setup_logging", Mock())
        monkeypatch.setattr(entry, "cmd_run", action)
        monkeypatch.setattr(entry, "cmd_chat", action)
    for value in (None, 0, 50):
        monkeypatch.setattr("sys.argv", args + ([] if value is None else ["--max-steps", str(value)]))
        entry.main()
        assert action.call_args.kwargs["max_steps"] == value
    action.reset_mock()
    for value in ("-1", "1.5", "true"):
        monkeypatch.setattr("sys.argv", args + ["--max-steps", value])
        with pytest.raises(SystemExit) as exc:
            entry.main()
        assert exc.value.code == 2
    action.assert_not_called()


# 功能：TUI 每轮发送同一启动上限，切换或恢复会话不丢失零值覆盖
# 设计：捕获实际发送方法的 IPC 参数，省略时要求字段不存在而不是偷偷使用本地配置
@pytest.mark.parametrize("limit", [None, 0, 50])
async def test_tui_per_message_payload(limit: int | None) -> None:
    app = KamaTuiApp("127.0.0.1", 1, max_steps=limit, resume_session_id="saved")
    client = Mock(send_command=AsyncMock())
    app._client = client
    for sid in ("saved", "new-session"):
        app._session_id = sid
        await app._do_send_message("test")
        method, params = client.send_command.call_args.args
        assert method == "session.send_message"
        assert params["session_id"] == sid
        assert ("max_steps" in params) == (limit is not None)
        assert params.get("max_steps") == limit


class _Client:
    # 捕获命令并提供无需网络的事件循环
    def __init__(self) -> None:
        self.calls = []
        self.handler = None
        self.messages_sent = 0
        self.sent = asyncio.Event()

    # 模拟连接建立
    async def connect(self) -> None:
        pass

    # 模拟连接关闭
    async def close(self) -> None:
        pass

    # 安装事件处理器
    def on_event(self, handler) -> None:
        self.handler = handler

    # 等待调用方取消事件循环
    async def run_event_loop(self) -> None:
        await asyncio.Event().wait()

    # 响应会话创建并为单次任务发出结束事件
    async def send_command(self, method, params):
        self.calls.append((method, params))
        if method == "agent.run":
            await self.handler({"type": "run.finished", "status": "success", "steps": 1})
        if method == "session.send_message":
            self.messages_sent += 1
            if self.messages_sent == 2:
                self.sent.set()
        return {"session_id": "s", "run_id": "r"}


# 功能：CLI 单次运行与多轮聊天都将参数发送给 Core，并保留显式零值
# 设计：替换网络连接但运行完整命令流程，聊天发送两条消息后退出以验证每轮携带参数
@pytest.mark.parametrize("limit", [None, 0, 50])
@pytest.mark.parametrize("mode", ["run", "chat"])
async def test_cli_payloads(mode: str, limit: int | None, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client()
    module = run if mode == "run" else chat
    monkeypatch.setattr(module, "SocketClient", Mock(return_value=client))
    if mode == "run":
        assert await run._run_async("test", KamaConfig(), max_steps=limit) == 0
        method = "agent.run"
    else:
        lines = iter(["first", "second"])

        # 两轮都送达后再模拟 EOF，避免关闭流程取消尚未开始的发送任务
        async def read(prompt):
            try:
                return next(lines)
            except StopIteration:
                await asyncio.wait_for(client.sent.wait(), 2)
                raise EOFError from None

        monkeypatch.setattr(chat, "_readline", read)
        assert await chat._chat_async(KamaConfig(), max_steps=limit) == 0
        method = "session.send_message"
    payloads = [params for name, params in client.calls if name == method]
    assert len(payloads) == (1 if mode == "run" else 2)
    for params in payloads:
        assert ("max_steps" in params) == (limit is not None)
        assert params.get("max_steps") == limit
