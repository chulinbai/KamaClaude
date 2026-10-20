from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from kama_claude.core.app import CoreApp
from kama_claude.core.config import KamaConfig
from kama_claude.core.llm.types import LlmResponse
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session import SessionManager, SessionStore
from kama_claude.core.session.manager import SESSION_IN_USE, SESSION_NOT_FOUND
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.socket_client import IpcError, SocketClient
from kama_claude.core.transport.socket_server import SocketServer


# 启动真实协议服务和指定工作区存储，仅替换 LLM 输出
@asynccontextmanager
async def _server(store: SessionStore, workspace: Path, port: int, provider: Mock):
    app = CoreApp()
    app._permission_manager = PermissionManager(timeout_s=0)
    app._sessions = SessionManager(
        store, lambda: AgentRunner(
            KamaConfig(), provider=provider, bus=app._bus,
            permission_manager=app._permission_manager,
        ), app._bus, workspace=workspace,
    )
    broadcaster = IpcEventBroadcaster()
    app._broadcaster = broadcaster
    app._bus.subscribe(broadcaster.handle)
    server = SocketServer("127.0.0.1", port, broadcaster, on_disconnect=app._client_disconnected)
    for method, handler in {
        "event.subscribe": app._subscribe_handler,
        "session.create": app._session_create_handler,
        "session.list": app._session_list_handler,
        "session.resume": app._session_resume_handler,
        "session.send_message": app._session_send_handler,
        "session.get_history": app._session_history_handler,
        "session.compact": app._session_compact_handler,
        "session.close": app._session_close_handler,
    }.items():
        server.register(method, handler)
    await server.start()
    try:
        yield app
    finally:
        await server.stop()


# 建立真实客户端连接并在退出时完整结束事件循环
@asynccontextmanager
async def _client(port: int):
    client = SocketClient("127.0.0.1", port)
    await client.connect()
    loop = asyncio.create_task(client.run_event_loop())
    try:
        yield client
    finally:
        await client.close()
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)


# 功能：真实 IPC 恢复与切换保持原会话身份，其他连接不能接管或修改
# 设计：两个 TCP 客户端竞争同一会话，检查错误码、连接归属、权限关闭标记和实际落盘记录
async def test_resume_owner_switch_and_failure_over_ipc(
    tmp_path: Path, free_port: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    store = SessionStore(tmp_path / "sessions")
    provider = Mock(chat=AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="answer")))
    async with _server(store, tmp_path, free_port, provider) as app:
        async with _client(free_port) as owner, _client(free_port) as other:
            created = await owner.send_command("session.create", {"mode": "chat", "title": "saved"})
            sid = created["session_id"]
            await owner.send_command("session.send_message", {"session_id": sid, "content": "first"})
            before = store.read_messages(sid)
            for method, params in (
                ("session.resume", {"session_id": sid}),
                ("session.send_message", {"session_id": sid, "content": "intruder"}),
                ("session.close", {"session_id": sid}),
                ("session.compact", {"session_id": sid}),
            ):
                with pytest.raises(IpcError) as exc:
                    await other.send_command(method, params)
                assert exc.value.code == SESSION_IN_USE
            listed = await other.send_command("session.list", {})
            assert listed["sessions"][0]["attached_elsewhere"]
            await owner.send_command("session.close", {"session_id": sid})
            assert app._permission_manager.is_closed(sid)
            current = (await owner.send_command("session.create", {}))["session_id"]
            with pytest.raises(IpcError) as exc:
                await owner.send_command("session.resume", {
                    "session_id": "missing", "current_session_id": current,
                })
            assert exc.value.code == SESSION_NOT_FOUND
            assert current in app._session_owners
            assert store.read_meta(current).status == "active"
            restored = await owner.send_command("session.resume", {
                "session_id": sid, "current_session_id": current,
            })
            assert restored["session_id"] == sid
            assert "messages" not in restored
            assert current not in app._session_owners
            assert store.read_meta(current).status == "closed"
            assert not app._permission_manager.is_closed(sid)
            assert app._permission_manager.is_closed(current)
            assert store.read_messages(sid) == before
            restored_meta = store.read_meta(sid)
            repeated = await owner.send_command("session.resume", {"session_id": sid})
            assert repeated == restored
            assert store.read_meta(sid) == restored_meta
            assert provider.chat.call_count == 1
            await owner.send_command("session.send_message", {"session_id": sid, "content": "next"})
            assert len(store.read_messages(sid)) == 4
            assert provider.chat.call_count == 2


# 功能：客户端断线和 Core 重启后均可恢复磁盘会话，后续模型输入带原笔记和历史
# 设计：在同一临时存储上启动两次真实服务，先验证断连清理，再跨服务实例恢复
async def test_resume_after_disconnect_and_core_restart(
    tmp_path: Path, free_port: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    store = SessionStore(tmp_path / "sessions")
    provider = Mock(chat=AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="answer")))
    async with _server(store, tmp_path, free_port, provider) as app:
        async with _client(free_port) as first:
            sid = (await first.send_command("session.create", {}))["session_id"]
            await first.send_command("session.send_message", {"session_id": sid, "content": "remember"})
            store.append_note(sid, "keep port 5433", "old-run")
        async with asyncio.timeout(2):
            while sid in app._session_owners:
                await asyncio.sleep(0.01)
        assert app._permission_manager.is_closed(sid)
        async with _client(free_port) as reconnected:
            result = await reconnected.send_command("session.resume", {"session_id": sid})
            assert result["session_id"] == sid
            assert not app._permission_manager.is_closed(sid)
            await reconnected.send_command("session.close", {"session_id": sid})

    async with _server(store, tmp_path, free_port, provider):
        async with _client(free_port) as restarted:
            result = await restarted.send_command("session.resume", {"session_id": sid})
            assert result["status"] == "waiting_for_input"
            await restarted.send_command("session.send_message", {"session_id": sid, "content": "continue"})
            call = provider.chat.call_args.kwargs
            assert call["messages"][0]["content"] == "remember"
            assert "keep port 5433" in call["system"]
            assert len(store.list_meta()) == 1
