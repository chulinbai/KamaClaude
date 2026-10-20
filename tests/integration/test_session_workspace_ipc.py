from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from kama_claude.core.bus.envelope import INVALID_PARAMS
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.session.manager import SESSION_PROJECT_MISMATCH
from kama_claude.core.session.store import SessionStore
from kama_claude.core.transport.socket_client import IpcError
from tests.integration.test_session_resume_ipc import _client, _server


# 两个真实 IPC 连接并发读写各自项目；Core 工作目录始终不变，恢复仍绑定原目录。
async def test_concurrent_client_workspaces_and_resume(
    tmp_path: Path, free_port: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = tmp_path / "core"
    core.mkdir()
    projects = {name: tmp_path / name for name in ("client a 中文", "client b")}
    for name, project in projects.items():
        (project / ".kama").mkdir(parents=True)
        (project / "input.txt").write_text(f"input:{name}", encoding="utf-8")
        (project / ".kama" / "context.md").write_text(f"context:{name}", encoding="utf-8")
    monkeypatch.chdir(core)
    arrived = set()
    both_running = asyncio.Event()
    seen = {}

    async def chat(**kwargs):
        name = kwargs["messages"][0]["content"]
        assert Path.cwd() == core
        if kwargs["step"] == 1:
            arrived.add(name)
            if len(arrived) == 2:
                both_running.set()
            await asyncio.wait_for(both_running.wait(), 3)
            seen[name] = kwargs["system"]
            return LlmResponse(stop_reason="tool_use", tool_calls=[
                ToolCallBlock("read", "read_file", {"path": "input.txt"}),
                ToolCallBlock("write", "write_file", {"path": "result.txt", "content": name}),
            ])
        return LlmResponse(stop_reason="end_turn", text="done")

    provider = Mock(chat=AsyncMock(side_effect=chat))
    store = SessionStore(tmp_path / "sessions")
    ids = {}
    async with _server(store, core, free_port, provider) as app:
        app._permission_manager._policies["write_file"] = ToolPolicy(PermissionDecision.ALLOW)
        async with _client(free_port) as first, _client(free_port) as second:
            clients = dict(zip(projects, (first, second), strict=True))
            for name, client in clients.items():
                created = await client.send_command("session.create", {
                    "mode": "chat", "workspace": str(projects[name]),
                })
                ids[name] = created["session_id"]
                assert store.read_meta(ids[name]).workspace == str(projects[name])
            await asyncio.wait_for(asyncio.gather(*(
                client.send_command("session.send_message", {"session_id": ids[name], "content": name})
                for name, client in clients.items()
            )), 8)
            for name, client in clients.items():
                assert (projects[name] / "result.txt").read_text(encoding="utf-8") == name
                assert f"context:{name}" in seen[name]
                other = next(value for value in projects if value != name)
                assert f"context:{other}" not in seen[name]
                results = [block for message in store.read_messages(ids[name])
                           if isinstance(message["content"], list) for block in message["content"]
                           if block.get("type") == "tool_result"]
                assert len(results) == 2
                assert not any(block.get("is_error") for block in results)
                assert f"input:{name}" in str(results)
                listed = await client.send_command("session.list", {"workspace": str(projects[name])})
                assert [s["session_id"] for s in listed["sessions"]] == [ids[name]]
                await client.send_command("session.close", {"session_id": ids[name]})
            assert not (core / "result.txt").exists()
            assert Path.cwd() == core
            with pytest.raises(IpcError) as exc:
                await first.send_command("session.create", {"workspace": "relative"})
            assert exc.value.code == INVALID_PARAMS

    # 重启 Core 后仍用客户端目录恢复，不能通过传入另一个目录改绑历史会话。
    async with _server(store, core, free_port, provider):
        async with _client(free_port) as client:
            name = next(iter(projects))
            with pytest.raises(IpcError) as exc:
                await client.send_command("session.resume", {"session_id": ids[name], "workspace": str(core)})
            assert exc.value.code == SESSION_PROJECT_MISMATCH
            restored = await client.send_command("session.resume", {
                "session_id": ids[name], "workspace": str(projects[name]),
            })
            assert restored["workspace"] == str(projects[name])
            assert len(store.list_meta()) == 2
