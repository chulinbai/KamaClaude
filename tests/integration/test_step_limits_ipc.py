from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from kama_claude.core.bus.envelope import INVALID_REQUEST
from kama_claude.core.bus.events import RunFinishedEvent
from kama_claude.core.config import KamaConfig
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.session.store import SessionStore
from kama_claude.core.transport.socket_client import IpcError
from tests.integration.test_session_resume_ipc import _client, _server


# 功能：真实 IPC 并发请求上限隔离，省略和空值继承配置，零值覆盖及单次运行正确生效
# 设计：两客户端同时进入模型回合，检查真实结束事件、会话元数据和非法输入无副作用
async def test_step_limits_across_clients_and_runs(tmp_path: Path, free_port: int) -> None:
    config = KamaConfig()
    config.agent.max_steps = 2
    started = set()
    both_started = asyncio.Event()
    finished = {}

    # 前两个运行在模型入口汇合，放大错误修改共享配置时的竞争窗口
    async def chat(*, step, run_id, **kwargs):
        if step == 1:
            started.add(run_id)
            if len(started) >= 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 3)
        await asyncio.sleep(0)
        if step == 26:
            return LlmResponse(stop_reason="end_turn", text="done")
        return LlmResponse(stop_reason="tool_use", tool_calls=[
            ToolCallBlock(f"step-{step}", "unregistered_test_tool", {}),
        ])

    # 保存 Core 的真实结束事件，避免仅根据响应是否成功判断上限行为
    async def collect(event) -> None:
        if isinstance(event, RunFinishedEvent):
            finished[event.run_id] = event

    store = SessionStore(tmp_path / "sessions")
    async with _server(store, tmp_path, free_port, Mock(chat=chat), config=config) as app:
        app._bus.subscribe(collect)
        async with _client(free_port) as first, _client(free_port) as second:
            first_sid = (await first.send_command("session.create", {}))["session_id"]
            second_sid = (await second.send_command("session.create", {}))["session_id"]
            results = await asyncio.wait_for(asyncio.gather(
                first.send_command("session.send_message", {
                    "session_id": first_sid, "content": "limited", "max_steps": 3,
                }),
                second.send_command("session.send_message", {
                    "session_id": second_sid, "content": "unlimited", "max_steps": 0,
                }),
            ), 5)
            assert finished[results[0]["run_id"]].steps == 3
            assert finished[results[0]["run_id"]].reason == "exceeded_max_steps"
            assert finished[results[1]["run_id"]].steps == 26
            assert finished[results[1]["run_id"]].status == "success"
            assert config.agent.max_steps == 2
            for optional in ({}, {"max_steps": None}):
                result = await second.send_command("session.send_message", {
                    "session_id": second_sid, "content": "inherit", **optional,
                })
                assert finished[result["run_id"]].steps == 2
            await second.send_command("session.close", {"session_id": second_sid})
            await second.send_command("session.resume", {"session_id": second_sid})
            result = await second.send_command("session.send_message", {
                "session_id": second_sid, "content": "after resume",
            })
            assert finished[result["run_id"]].steps == 2
            metadata = json.loads((store.session_dir(second_sid) / "meta.json").read_text())
            assert "max_steps" not in metadata

            before = store.read_messages(first_sid)
            for invalid in (-1, True, False, 1.5, "3"):
                with pytest.raises(IpcError) as exc:
                    await first.send_command("session.send_message", {
                        "session_id": first_sid, "content": "invalid", "max_steps": invalid,
                    })
                assert exc.value.code == INVALID_REQUEST
            assert store.read_messages(first_sid) == before

            for optional, expected in (({}, 2), ({"max_steps": 0}, 26), ({"max_steps": 4}, 4)):
                result = await first.send_command("agent.run", {"goal": "single", **optional})
                await asyncio.wait_for(asyncio.gather(*app._running_runs), 5)
                assert finished[result["run_id"]].steps == expected
            assert config.agent.max_steps == 2
