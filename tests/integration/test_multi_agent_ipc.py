from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from kama_claude.core.app import CoreApp
from kama_claude.core.bus.envelope import INVALID_PARAMS
from kama_claude.core.config import KamaConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.llm.types import LlmResponse
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session import SessionManager, SessionStore
from kama_claude.core.session.manager import SESSION_IN_USE
from kama_claude.core.transport.socket_client import IpcError, SocketClient
from kama_claude.core.transport.socket_server import SocketServer


# 建立真实 IPC 客户端并在退出时收回事件循环
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


# 功能：专用 IPC 支持查看、消息、追加和关闭，同时阻止其他连接及跨会话目标访问
# 设计：两个真实 TCP 连接各持有会话，通过同一注册表查询确认协议操作实际生效
async def test_multi_agent_controls_and_session_ownership(tmp_path: Path, free_port: int) -> None:
    app = CoreApp()
    provider = Mock(chat=AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="done")))
    app._sessions = SessionManager(
        SessionStore(tmp_path / "sessions"),
        lambda: AgentRunner(KamaConfig(), provider=provider, bus=app._bus),
        app._bus,
        workspace=tmp_path,
    )
    server = SocketServer("127.0.0.1", free_port)
    for method, handler in {
        "session.create": app._session_create_handler,
        "subagent.list": app._subagent_list_handler,
        "subagent.result": app._subagent_result_handler,
        "subagent.send_input": app._subagent_send_handler,
        "subagent.close": app._subagent_close_handler,
    }.items():
        server.register(method, handler)
    await server.start()
    try:
        async with _client(free_port) as owner, _client(free_port) as other:
            sid = (await owner.send_command("session.create", {}))["session_id"]
            other_sid = (await other.send_command("session.create", {}))["session_id"]
            registry = app._sessions.agent_registry(sid)

            # 复用消费入口生成能够追加任务的简短执行
            async def execute(context: ExecutionContext) -> None:
                await registry.consume(context)
                context.result = context.goal
                context.mark_success()

            agent, run = await registry.spawn(
                "main",
                "worker",
                "start",
                None,
                execute,
                2,
                tmp_path,
            )
            await registry.join(run)
            queries = [
                ("subagent.list", {}),
                ("subagent.result", {"agent_id": agent}),
                ("subagent.send_input", {"agent_id": agent, "message": "intruder"}),
                ("subagent.close", {"agent_id": agent}),
            ]
            for method, params in queries:
                with pytest.raises(IpcError) as exc:
                    await other.send_command(method, {"session_id": sid, **params})
                assert exc.value.code == SESSION_IN_USE
            with pytest.raises(IpcError) as exc:
                await other.send_command(
                    "subagent.send_input",
                    {
                        "session_id": other_sid,
                        "agent_id": agent,
                        "message": "cross-session",
                    },
                )
            assert exc.value.code == INVALID_PARAMS
            listing = await owner.send_command("subagent.list", {"session_id": sid})
            assert {a["agent_id"] for a in listing["agents"]} == {"main", agent}
            sent = await owner.send_command(
                "subagent.send_input",
                {
                    "session_id": sid,
                    "agent_id": agent,
                    "message": "queued",
                },
            )
            assert sent["data"]["pending_messages"] == 1
            assert registry.agents[agent].state == "IDLE"
            next_run = await owner.send_command(
                "subagent.send_input",
                {
                    "session_id": sid,
                    "agent_id": agent,
                    "message": "next",
                    "wake": True,
                },
            )
            next_id = next_run["data"]["run_id"]
            await registry.join(next_id)
            result = await owner.send_command(
                "subagent.result",
                {
                    "session_id": sid,
                    "run_id": run,
                },
            )
            assert result["data"]["result"] == "start"
            closed = await owner.send_command(
                "subagent.close",
                {
                    "session_id": sid,
                    "agent_id": agent,
                },
            )
            assert closed["data"]["state"] == "CLOSED"
            assert registry.agents[agent].context.messages == []
    finally:
        await app._sessions.shutdown()
        await server.stop()
