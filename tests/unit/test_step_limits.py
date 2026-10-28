from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from kama_claude.core.bus.commands import AgentRunCommand, SessionSendMessageCommand
from kama_claude.core.config import KamaConfig, _apply_env, _apply_toml
from kama_claude.core.context import AgentContext, ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.loop import AgentLoop
from kama_claude.core.runner import AgentRunner
from kama_claude.core.subagent.registry import AgentRuntime
from kama_claude.core.tools.registry import ToolRegistry


# 返回若干工具调用后正常完成，所有步骤均不产生外部副作用
def _provider(finish_at: int = 26) -> Mock:
    # 依据每轮实际步数返回响应，使测试同时覆盖计步重置
    async def chat(*, step: int, **kwargs) -> LlmResponse:
        if step >= finish_at:
            return LlmResponse(stop_reason="end_turn", text="done")
        return LlmResponse(stop_reason="tool_use", tool_calls=[
            ToolCallBlock(f"step-{step}", "unregistered_test_tool", {}),
        ])

    return Mock(chat=AsyncMock(side_effect=chat))


# 功能：所有默认入口统一为无限步数，执行超过原有二十步仍能正常完成
# 设计：让模拟模型在第二十六步结束，同时检查上下文和子运行的默认设置
async def test_unlimited_defaults_run_past_twenty() -> None:
    assert KamaConfig().agent.max_steps is None
    assert AgentRuntime(AgentContext()).max_steps is None
    context = ExecutionContext("r", "continue")
    assert context.max_steps is None
    provider = _provider()
    await AgentLoop(provider, ToolRegistry(), EventBus()).run(context)
    assert (context.status, context.step) == ("success", 26)
    assert provider.chat.await_count == 26


# 功能：正数限制精确生效，最后一步完成时优先返回成功
# 设计：仅改变模型完成时机，区分到达上限与成功完成的同一步边界
@pytest.mark.parametrize("finish_at,status,reason", [
    (3, "failed", "exceeded_max_steps"), (2, "success", None),
])
async def test_step_boundary(finish_at: int, status: str, reason: str | None) -> None:
    context = ExecutionContext("r", "test", max_steps=2)
    await AgentLoop(_provider(finish_at), ToolRegistry(), EventBus()).run(context)
    assert context.step == 2
    assert (context.status, context.reason) == (status, reason)


# 功能：无限模式仍能通过取消及模型错误终止
# 设计：直接在模型边界注入取消或异常，避免靠步数上限掩盖退出逻辑问题
@pytest.mark.parametrize("cancel", [True, False])
async def test_unlimited_cancellation_and_error(cancel: bool) -> None:
    context = ExecutionContext("r", "test")
    error = asyncio.CancelledError() if cancel else RuntimeError("model unavailable")
    loop = AgentLoop(Mock(chat=AsyncMock(side_effect=error)), ToolRegistry(), EventBus())
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await loop.run(context)
    else:
        await loop.run(context)
    assert context.status == "failed"
    assert context.reason == ("cancelled" if cancel else "llm_error")


# 功能：配置中的零值规范化为无限，环境变量能覆盖已有 TOML 上限
# 设计：直接走实际配置解析函数，独立于用户机器配置检验来源优先级
def test_config_zero_and_env_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    config = KamaConfig()
    _apply_toml(config, {"agent": {"max_steps": 7}})
    assert config.agent.max_steps == 7
    monkeypatch.setenv("KAMA_MAX_STEPS", "0")
    _apply_env(config)
    assert config.agent.max_steps is None
    monkeypatch.setenv("KAMA_MAX_STEPS", "12")
    _apply_env(config)
    assert config.agent.max_steps == 12
    _apply_toml(config, {"agent": {"max_steps": 0}})
    assert config.agent.max_steps is None


# 功能：TOML 不接受布尔值、负数、浮点数及字符串作为上限
# 设计：特别覆盖 Python 把 bool 视为 int 的陷阱，防止 true 被解释为一步
@pytest.mark.parametrize("value", [True, False, -1, 1.0, "10"])
def test_invalid_toml_limits(value: object) -> None:
    with pytest.raises(SystemExit, match="non-negative integer"):
        _apply_toml(KamaConfig(), {"agent": {"max_steps": value}})


# 功能：环境变量拒绝负数、浮点数、布尔字符串与空字符串
# 设计：保持环境变量作为文本的真实输入形态，检查解析失败发生在启动阶段
@pytest.mark.parametrize("value", ["-1", "1.5", "true", "false", ""])
def test_invalid_env_limits(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAMA_MAX_STEPS", value)
    with pytest.raises(SystemExit, match="KAMA_MAX_STEPS"):
        _apply_env(KamaConfig())


# 功能：两个 IPC 请求允许省略、空值、零与正数，并严格拒绝隐式转换
# 设计：用相同数据集检查两个公共请求模型，避免入口校验出现差异
@pytest.mark.parametrize("model,fields", [
    (AgentRunCommand, {"goal": "test"}),
    (SessionSendMessageCommand, {"session_id": "s", "content": "test"}),
])
def test_wire_limit_validation(model, fields: dict) -> None:
    assert model.model_validate(fields).max_steps is None
    for value in (None, 0, 1, 50):
        assert model.model_validate({**fields, "max_steps": value}).max_steps == value
    for value in (True, False, -1, 1.0, "10"):
        with pytest.raises(ValidationError):
            model.model_validate({**fields, "max_steps": value})


# 功能：请求覆盖只影响本轮，零值能取消 Core 上限且下一轮省略时恢复继承
# 设计：复用同一个 Runner 和配置连续运行，验证计步、结果以及共享配置未被修改
@pytest.mark.parametrize("override,expected", [(None, 2), (0, 26), (5, 5)])
async def test_runner_override_is_per_run(tmp_path: Path, override: int | None, expected: int) -> None:
    config = KamaConfig()
    config.agent.max_steps = 2
    provider = _provider()
    runner = AgentRunner(config, provider=provider, runs_dir=tmp_path / "runs")
    try:
        outcome = await runner.run_and_capture("test", run_id="first", max_steps=override)
        context = runner.background_tasks.runs["first"].context
        assert context.step == expected
        assert outcome.status == ("success" if override == 0 else "failed")
        assert config.agent.max_steps == 2
        await runner.run_and_capture("another turn", run_id="second")
        assert runner.background_tasks.runs["second"].context.step == 2
        assert runner.background_tasks.runs["second"].context.max_steps == 2
    finally:
        await runner.background_tasks.shutdown()


# 功能：子 Agent 继承父运行覆盖值，唤醒后独立重计步且无限模式能消费结束时的消息
# 设计：通过实际 spawn 工具与共用 Runner 创建子任务，配置故意设为不同值以捕捉错误回退
@pytest.mark.parametrize("override,child_steps", [(0, 26), (2, 2)])
async def test_child_inherits_override_and_wakes(
    tmp_path: Path, override: int, child_steps: int,
) -> None:
    config = KamaConfig()
    config.agent.max_steps = 1
    injected = False
    runner: AgentRunner

    # 父任务创建子任务，子任务中注入尾部消息以覆盖收件箱的无限分支
    async def chat(*, messages, step, run_id, **kwargs):
        nonlocal injected
        if messages[0]["content"] == "root":
            if step == 1:
                return LlmResponse(stop_reason="tool_use", tool_calls=[
                    ToolCallBlock("spawn", "spawn_agent", {"description": "child", "prompt": "child"}),
                ])
            return LlmResponse(stop_reason="end_turn", text="parent done")
        if override == 0 and not injected and step == 25:
            registry = runner.background_tasks
            child_id = registry.runs[run_id].context.agent_context.agent_id
            await registry.send("main", child_id, "late message")
            injected = True
            return LlmResponse(stop_reason="end_turn", text="continue after inbox")
        if step >= 26:
            return LlmResponse(stop_reason="end_turn", text="child done")
        return LlmResponse(stop_reason="tool_use", tool_calls=[
            ToolCallBlock(f"child-{step}", "unregistered_test_tool", {}),
        ])

    runner = AgentRunner(config, provider=Mock(chat=chat), runs_dir=tmp_path / "runs")
    try:
        await runner.run_and_capture("root", run_id="root", max_steps=override)
        registry = runner.background_tasks
        child_id = next(key for key in registry.agents if key != "main")
        child = registry.agents[child_id]
        first = registry.runs[child.latest_run_id].context
        assert child.max_steps == (None if override == 0 else override)
        assert first.step == child_steps
        assert registry.runs["root"].context.step == 2
        if override == 0:
            assert not child.inbox
            assert "late message" in str(first.messages)
        resumed = await registry.send("main", child_id, "wake again", wake=True)
        await registry.join(resumed["run_id"])
        second = registry.runs[resumed["run_id"]].context
        assert second is not first
        assert second.step == child_steps
        assert second.max_steps == first.max_steps
        assert config.agent.max_steps == 1
    finally:
        await runner.background_tasks.shutdown()
