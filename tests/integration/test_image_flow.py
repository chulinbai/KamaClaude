from __future__ import annotations

import asyncio
import base64
import io
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import anthropic
import pytest
from PIL import Image

from kama_claude.core.config import KamaConfig, get_config
from kama_claude.core.events.bus import EventBus
from kama_claude.core.images import image_references
from kama_claude.core.llm.provider import AnthropicProvider
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.mcp.client import McpCallResult, McpClient, McpToolDef
from kama_claude.core.mcp.tool import McpTool
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.trace.writer import TraceWriter


# 生成无私有信息的纯色测试图片
def _picture(color: str) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (48, 36), color).save(stream, format="PNG")
    return stream.getvalue()


# 构造共享真实运行内核的会话，模型服务由测试注入
def _manager(root: Path, provider, config, trace, mcp_manager=None) -> SessionManager:
    bus = EventBus()
    return SessionManager(
        SessionStore(root / "sessions"),
        lambda: AgentRunner(config, provider=provider, bus=bus, trace=trace,
                            mcp_manager=mcp_manager),
        bus, provider=provider, workspace=root,
    )


# 功能：自然语言路径、多图上下文、重启恢复和压缩后重新读图串成完整会话
# 设计：只替换外部模型决策，真实执行文件权限、工具、缓存、持久化、压缩与 trace
@pytest.mark.parametrize("automatic", [False, True])
async def test_image_session_restore_and_compact(tmp_path: Path, automatic: bool) -> None:
    red, blue = tmp_path / "中文 红.png", tmp_path / "blue.png"
    red.write_bytes(_picture("red"))
    blue.write_bytes(_picture("blue"))
    config = KamaConfig()
    config.compaction.auto_threshold = 0.8 if automatic else 0
    calls = []
    compact_calls = []
    image_id = ""

    # 检查模型看到真实图片块，并按阶段模拟模型调用和最终回答
    async def chat(messages, tool_schemas, bus, run_id, **kwargs):
        payload = json.dumps(messages)
        assert "image_ref" not in payload
        if run_id == "compact":
            compact_calls.append(messages)
            assert '"type": "image"' in payload
            return LlmResponse(stop_reason="end_turn", text="The images show red and blue.")
        calls.append(messages)
        if len(calls) == 1:
            assert "比较" in messages[-1]["content"]
            assert "read_image" in {tool["name"] for tool in tool_schemas}
            return LlmResponse(stop_reason="tool_use", tool_calls=[
                ToolCallBlock("red", "read_image", {"path": red.name}),
                ToolCallBlock("blue", "read_image", {"path": str(blue)}),
            ], usage=UsageStats(100, 20, context_pct=0.9 if automatic else 0.1))
        if len(calls) == 4:
            return LlmResponse(stop_reason="tool_use", tool_calls=[
                ToolCallBlock("revisit", "read_image", {"image_id": image_id}),
            ])
        if not automatic or len(calls) == 5:
            assert '"type": "image"' in payload
        return LlmResponse(stop_reason="end_turn", text="red and blue")

    provider = Mock(chat=chat)
    trace = TraceWriter(tmp_path / "trace.jsonl")
    await trace.start()
    manager = _manager(tmp_path, provider, config, trace)
    sid = (await manager.create("chat")).id
    try:
        await manager.send_message(sid, f"比较 {red.name} 和 {blue} 的颜色", run_id="first")
        store = SessionStore(tmp_path / "sessions")
        history = store.read_messages(sid)
        if automatic:
            image_id = next((store.session_dir(sid) / "images").glob("*.bin")).stem
            assert len(compact_calls) == 1
        else:
            refs = list(image_references(history))
            assert len(refs) == 2
            image_id = refs[0].image_id
        red.unlink()
        blue.unlink()
        await manager.close(sid)
        await manager.shutdown()
        manager = _manager(tmp_path, provider, config, trace)
        await manager.resume(sid)
        await manager.send_message(sid, "继续说明刚才的图片", run_id="restored")
        if not automatic:
            await manager.compact(sid)
        assert image_id in str(store.read_messages(sid))
        await manager.send_message(sid, "再看一下缓存图片", run_id="revisit")
        assert len(calls) == 5
        assert list(image_references(store.read_messages(sid)))[0].image_id == image_id
    finally:
        await manager.shutdown()
        await trace.stop()
    for path in [tmp_path / "trace.jsonl", *store.session_dir(sid).rglob("*.jsonl")]:
        assert '"base64"' not in path.read_text(encoding="utf-8")


# 功能：真实 MCP TCP 结果中的图片与路径参数可穿过完整 Agent 内核
# 设计：使用本机协议服务器，模型第一轮调用 MCP，第二轮验证解码后的图片像素
async def test_mcp_image_tcp_pipeline(tmp_path: Path) -> None:
    raw = _picture("red")
    received = []

    # 模拟独立 MCP 服务端并处理握手、发现与图片工具调用
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                request = json.loads(line)
                if "id" not in request:
                    continue
                method = request["method"]
                if method == "tools/list":
                    result = {"tools": [{"name": "analyze", "description": "Read a local image",
                                         "inputSchema": {"type": "object", "properties": {
                                             "path": {"type": "string"}}, "required": ["path"]}}]}
                elif method == "tools/call":
                    received.append(request["params"])
                    result = {"content": [
                        {"type": "text", "text": "The requested image"},
                        {"type": "image", "mimeType": "image/png",
                         "data": base64.b64encode(raw).decode()},
                    ], "isError": False}
                else:
                    result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "test", "version": "1"}}
                writer.write((json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                          "result": result}) + "\n").encode())
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    client = McpClient()
    try:
        await client.connect_tcp("127.0.0.1", server.sockets[0].getsockname()[1])
        definition = (await client.list_tools())[0]
        tool = McpTool(client, "local", definition)
        calls = []
        source = str(tmp_path / "图.png")

        # 保留路径输入并确认模型收到的真实内容与服务端图片完全相同
        async def chat(messages, **kwargs):
            calls.append(messages)
            if len(calls) == 1:
                return LlmResponse(stop_reason="tool_use", tool_calls=[
                    ToolCallBlock("mcp", tool.name, {"path": source}),
                ])
            image = messages[-1]["content"][0]["content"][-1]
            assert base64.b64decode(image["source"]["data"]) == raw
            return LlmResponse(stop_reason="end_turn", text="red")

        runner = AgentRunner(KamaConfig(), provider=Mock(chat=chat), runs_dir=tmp_path / "runs",
                             mcp_manager=Mock(get_tools=Mock(return_value=[tool])))
        result = await runner.run_and_capture(f"使用 MCP 分析 {source}", run_id="mcp")
        assert result.status == "success"
        assert received == [{"name": "analyze", "arguments": {"path": source}}]
        assert tool._images is None
        assert len(list((tmp_path / "runs" / "mcp" / "images").glob("*.bin"))) == 1
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


# 功能：可选的真实模型验收验证路径读取后能识别图片颜色
# 设计：仅显式启用时发送合成纯色图片，限制轮数和超时，不读取用户私有图片
@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("KAMA_TEST_VISION") != "1", reason="Set KAMA_TEST_VISION=1")
@pytest.mark.parametrize("via_mcp", [False, True])
async def test_live_image_model(tmp_path: Path, via_mcp: bool) -> None:
    config = get_config()
    config.agent.max_steps = 3
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY is not configured")
    source = tmp_path / "sample.png"
    source.write_bytes(_picture("red"))
    client = anthropic.AsyncAnthropic(timeout=30, max_retries=0)
    provider = AnthropicProvider(config.llm.default_model, client=client)
    events = []

    # 收集事件以便在失败时只报告安全摘要
    async def collect(event) -> None:
        events.append(event.model_dump())

    mcp_manager = None
    expected_tool = "read_image"
    prompt = f"请查看图片 {source} 并用英语回答主要颜色，只回答颜色名称。"
    if via_mcp:
        mcp_client = Mock(call_tool=AsyncMock(return_value=McpCallResult(content=[
            {"type": "image", "mimeType": "image/png",
             "data": base64.b64encode(source.read_bytes()).decode()},
        ])))
        tool = McpTool(mcp_client, "image_test", McpToolDef(
            "read", "Read a local image by path and return its image content.",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        ))
        mcp_manager = Mock(get_tools=Mock(return_value=[tool]))
        expected_tool = tool.name
        prompt = f"请调用 {expected_tool} 查看图片 {source} 并用英语回答主要颜色，只回答颜色名称。"
    manager = _manager(tmp_path, provider, config, None, mcp_manager)
    manager._bus.subscribe(collect)
    sid = (await manager.create("chat")).id
    try:
        await asyncio.wait_for(manager.send_message(
            sid, prompt, run_id="live",
        ), timeout=100)
        assert any(e["type"] == "tool.call_finished" and e["tool_name"] == expected_tool
                   for e in events), "The model did not read the image"
        finished = [e for e in events if e["type"] == "run.finished"][-1]
        assert finished["status"] == "success", str([
            e.get("message", e.get("reason", "")) for e in events
            if e["type"] in ("log.line", "run.finished")
        ])
        assert "red" in str((await manager.get_history(sid))[-1]).lower()
    finally:
        await manager.shutdown()
        await client.close()
