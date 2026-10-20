from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from kama_claude.core.app import CoreApp
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.paths import PathAccessController
from kama_claude.core.session import SessionManager, SessionStore
from kama_claude.core.tools.builtin import CreateDirTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.socket_client import SocketClient
from kama_claude.core.transport.socket_server import SocketServer


# 功能：真实 IPC 绑定审批请求与所属连接，断连清理忙碌会话的待审批和路径授权
# 设计：两个 TCP 客户端竞争同一请求，批准后再次挂起并断开所属连接，检查实际目录和内存
async def test_ipc_response_owner_and_disconnect(tmp_path: Path, free_port: int) -> None:
    work = tmp_path / "project"
    work.mkdir()
    target = tmp_path / "test_file"
    app = CoreApp()
    manager = PermissionManager(timeout_s=0)
    app._permission_manager = manager
    app._sessions = SessionManager(SessionStore(tmp_path / "sessions"), lambda: None, app._bus)
    broadcaster = IpcEventBroadcaster()
    app._broadcaster = broadcaster
    app._bus.subscribe(broadcaster.handle)
    server = SocketServer("127.0.0.1", free_port, broadcaster,
                          on_disconnect=app._client_disconnected)
    server.register("event.subscribe", app._subscribe_handler)
    server.register("session.create", app._session_create_handler)
    server.register("permission.respond", app._permission_respond_handler)
    server.register("session.close", app._session_close_handler)
    await server.start()
    client = SocketClient("127.0.0.1", free_port)
    intruder = SocketClient("127.0.0.1", free_port)
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # 仅收集审批事件，不能在事件回调中阻塞等待同一 socket 的 RPC 响应
    async def collect(event: dict[str, Any]) -> None:
        if event.get("type") == "permission.requested":
            await events.put(event)

    client.on_event(collect)
    await client.connect()
    await intruder.connect()
    loops = [asyncio.create_task(c.run_event_loop()) for c in (client, intruder)]
    calls: list[asyncio.Task[Any]] = []
    try:
        await client.send_command("event.subscribe", {"topics": ["permission.*"], "scope": "global"})
        session = await client.send_command("session.create", {"mode": "chat"})
        sid = session["session_id"]
        registry = ToolRegistry()
        registry.register(CreateDirTool(PathAccessController(work)))
        task = asyncio.create_task(invoke_tool(registry, ToolCallBlock(
            id="t", name="create_dir", input={"path": str(target)},
        ), app._bus, "r", permission_manager=manager, session_id=sid))
        calls.append(task)
        event = await asyncio.wait_for(events.get(), 2)
        assert event["paths"][0]["path"] == str(target)
        assert not target.exists()
        params = {"tool_use_id": "t", "request_id": event["request_id"],
                  "decision": "allow_session", "session_id": sid}
        assert not (await intruder.send_command("permission.respond", params))["ok"]
        assert not (await client.send_command("permission.respond", {
            "tool_use_id": "t", "decision": "always_allow",
        }))["ok"]
        assert (await client.send_command("permission.respond", params))["ok"]
        assert not (await asyncio.wait_for(task, 2)).is_error
        assert manager.session_grants(sid, str(work))
        second = tmp_path / "second"
        task = asyncio.create_task(invoke_tool(registry, ToolCallBlock(
            id="t", name="create_dir", input={"path": str(second)},
        ), app._bus, "r2", permission_manager=manager, session_id=sid))
        calls.append(task)
        event = await asyncio.wait_for(events.get(), 2)
        lock = app._sessions._locks[sid]
        await lock.acquire()
        try:
            await client.close()
            assert (await asyncio.wait_for(task, 2)).is_error
        finally:
            lock.release()
        assert not second.exists()
        assert not manager.session_grants(sid, str(work))
        assert not manager._pending
        assert not manager.respond("t", "allow_session", event["request_id"])
        new_session = await intruder.send_command("session.create", {"mode": "chat"})
        assert not manager.session_grants(new_session["session_id"], str(work))
        await intruder.send_command("session.close", {"session_id": new_session["session_id"]})
    finally:
        await client.close()
        await intruder.close()
        for task in calls + loops:
            task.cancel()
        await asyncio.gather(*calls, *loops, return_exceptions=True)
        await server.stop()
