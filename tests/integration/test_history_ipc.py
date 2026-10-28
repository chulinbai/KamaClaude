from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.app import CoreApp
from kama_claude.core.history.index import SearchStopped
from kama_claude.core.history.models import HistoryConfig
from kama_claude.core.history.service import HistoryService
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore
from kama_claude.core.transport.socket_client import IpcError, SocketClient
from kama_claude.core.transport.socket_server import SocketServer


# 在临时目录准备真实 IPC 服务与两个不同工作区的历史
async def setup_history(tmp_path: Path, port: int) -> tuple[CoreApp, SocketServer, SocketClient]:
    app = CoreApp()
    store = SessionStore(tmp_path / "sessions", HistoryConfig(segment_max_bytes=300))
    for sid, workspace in (("sess-one", tmp_path), ("sess-two", tmp_path / "other")):
        store.write_meta(Session(
            id=sid, title=sid, mode="chat", status="closed", workspace=str(workspace),
            created_at="2026-10-28T00:00:00+00:00", updated_at="2026-10-28T00:00:00+00:00",
        ))
        store.history(sid).emit("user_message", {"content": f"needle {sid}"})
    app._history = HistoryService(store)
    app._permission_manager = PermissionManager(timeout_s=1)
    server = SocketServer("127.0.0.1", port, on_disconnect=app._client_disconnected)
    for name in ("search", "read", "status", "reindex"):
        server.register(f"history.{name}", getattr(app, f"_history_{name}_handler"))
    server.register("permission.respond", app._permission_respond_handler)
    await server.start()
    client = SocketClient("127.0.0.1", port)
    await client.connect()
    return app, server, client


# 功能：独立 CLI 协议不创建会话也能搜索、读取和查询分片状态
# 设计：通过真实 TCP 和 Pydantic 响应走完整路径，检查未知 ID 不扩大范围
async def test_history_search_read_status_ipc(tmp_path: Path, free_port: int) -> None:
    app, server, client = await setup_history(tmp_path, free_port)
    receiver = asyncio.create_task(client.run_event_loop())
    try:
        result = await client.send_command("history.search", {
            "query": "needle", "workspace": str(tmp_path),
        })
        assert result["complete"] and len(result["results"]) == 1
        record_id = result["results"][0]["record_id"]
        read = await client.send_command("history.read", {
            "record_id": record_id, "workspace": str(tmp_path),
        })
        assert "needle sess-one" in read["text"]
        status = await client.send_command("history.status", {"workspace": str(tmp_path)})
        assert status["sessions"] == 1 and status["segments"] == 1
        assert app._session_owners == {}
        with pytest.raises(IpcError) as error:
            await client.send_command("history.read", {
                "record_id": "sess-two:1", "workspace": str(tmp_path),
            })
        assert error.value.code == -32031
    finally:
        await client.close()
        await receiver
        await server.stop()
        assert app._history is not None
        await app._history.close()


# 功能：跨工作区审批绑定连接，其他连接不能代答，连接授权可复用于读取
# 设计：实际接收审批推送后先用第二客户端尝试批准，再由请求者批准并读取另一工作区
async def test_history_scope_approval_owned_by_connection(
    tmp_path: Path, free_port: int,
) -> None:
    app, server, client = await setup_history(tmp_path, free_port)
    stranger = SocketClient("127.0.0.1", free_port)
    await stranger.connect()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # 保存审批推送以便测试在另一个连接上模拟越权响应
    async def event_received(event: dict[str, Any]) -> None:
        if event.get("type") == "permission.requested":
            await queue.put(event)

    client.on_event(event_received)
    receivers = [asyncio.create_task(c.run_event_loop()) for c in (client, stranger)]
    try:
        request = asyncio.create_task(client.send_command("history.search", {
            "query": "needle", "workspace": str(tmp_path), "scope": "all",
        }))
        event = await asyncio.wait_for(queue.get(), 2)
        assert event["reason"] == "history_scope"
        assert event["allowed_decisions"] == ["allow_once", "allow_session", "deny_once"]
        response = {
            "request_id": event["request_id"], "tool_use_id": event["tool_use_id"],
            "session_id": event["session_id"], "decision": "allow_session",
        }
        legacy_response = {key: value for key, value in response.items() if key != "request_id"}
        assert not (await stranger.send_command("permission.respond", legacy_response))["ok"]
        assert not (await stranger.send_command("permission.respond", response))["ok"]
        assert (await client.send_command("permission.respond", response))["ok"]
        result = await request
        assert len(result["results"]) == 2
        read = await client.send_command("history.read", {
            "record_id": "sess-two:1", "workspace": str(tmp_path), "scope": "all",
        })
        assert "needle sess-two" in read["text"]
        assert queue.empty()
    finally:
        await client.close()
        await stranger.close()
        await asyncio.gather(*receivers)
        await server.stop()
        assert app._history is not None
        await app._history.close()


# 功能：未交互批准的全局检索明确拒绝，不把 scope=all 视为授权
# 设计：收到审批后拒绝，验证错误码且主会话存储不产生临时聊天记录
async def test_history_scope_denied(tmp_path: Path, free_port: int) -> None:
    app, server, client = await setup_history(tmp_path, free_port)
    responses: list[asyncio.Task[dict[str, Any]]] = []

    # 异步提交拒绝，事件读取循环继续处理审批响应
    async def event_received(event: dict[str, Any]) -> None:
        if event.get("type") == "permission.requested":
            responses.append(asyncio.create_task(client.send_command("permission.respond", {
                "request_id": event["request_id"], "tool_use_id": event["tool_use_id"],
                "session_id": event["session_id"], "decision": "deny_once",
            })))

    client.on_event(event_received)
    receiver = asyncio.create_task(client.run_event_loop())
    try:
        with pytest.raises(IpcError) as error:
            await client.send_command("history.search", {
                "query": "needle", "workspace": str(tmp_path), "scope": "all",
            })
        assert error.value.code == -32031
        await asyncio.gather(*responses)
        assert app._session_owners == {}
    finally:
        await client.close()
        await receiver
        await server.stop()
        assert app._history is not None
        await app._history.close()


# 功能：客户端断连撤销连接授权并停止实际后台查询
# 设计：使工作线程停在可取消的读取点，关闭 TCP 后验证退出信号而非仅检查协程状态
async def test_disconnect_stops_history_worker(
    tmp_path: Path, free_port: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, server, client = await setup_history(tmp_path, free_port)
    assert app._history is not None
    started, finished = threading.Event(), threading.Event()

    # 模拟一个等待读取的扫描线程，收到取消必须真实退出
    def sources(sessions: Any, stop: threading.Event, deadline: float, warnings: Any) -> Any:
        started.set()
        stop.wait(5)
        finished.set()
        raise SearchStopped()

    monkeypatch.setattr(app._history, "_sources", sources)
    receiver = asyncio.create_task(client.run_event_loop())
    request = asyncio.create_task(client.send_command("history.search", {
        "query": "needle", "workspace": str(tmp_path),
    }))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await client.close()
        await receiver
        assert await asyncio.to_thread(finished.wait, 2)
        await asyncio.gather(request, return_exceptions=True)
    finally:
        await client.close()
        await server.stop()
        await app._history.close()


# 功能：真实 CLI 进程完成搜索、读取及状态命令，非交互全局查询明确拒绝
# 设计：连接临时 TCP 服务并解析 stdout JSON，覆盖参数分发、输出编码与审批接收
async def test_history_cli_subprocess(tmp_path: Path, free_port: int) -> None:
    app, server, client = await setup_history(tmp_path, free_port)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("KAMA_")}
    environment.update({
        "KAMA_HOST": "127.0.0.1", "KAMA_PORT": str(free_port),
        "KAMA_CONFIG": str(tmp_path / "absent.toml"),
        "KAMA_LOG_FILE": str(tmp_path / "cli.log"), "PYTHONIOENCODING": "utf-8",
        "USERPROFILE": str(tmp_path), "HOME": str(tmp_path),
    })

    # 启动独立入口进程，始终回收子进程以免测试失败后残留客户端
    async def run(*arguments: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "from kama_claude.cli.main import main; main()",
            "history", *arguments, "--workspace", str(tmp_path), "--json",
            env=environment, cwd=tmp_path, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
            return process.returncode or 0, stdout.decode("utf-8"), stderr.decode("utf-8")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    try:
        code, stdout, stderr = await run("search", "needle")
        assert code == 0, stderr
        result = json.loads(stdout)
        assert result["complete"] and len(result["results"]) == 1
        code, stdout, stderr = await run("read", result["results"][0]["record_id"])
        assert code == 0 and "needle sess-one" in json.loads(stdout)["text"], stderr
        code, stdout, stderr = await run("status")
        assert code == 0 and json.loads(stdout)["sessions"] == 1, stderr
        code, stdout, stderr = await run("search", "needle", "--all")
        assert code == 1 and stdout == "" and "-32031" in stderr
        assert app._session_owners == {}
    finally:
        await client.close()
        await server.stop()
        assert app._history is not None
        await app._history.close()
