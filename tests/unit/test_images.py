from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import anthropic
import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from kama_claude.core.bus.events import PermissionRequestedEvent
from kama_claude.core.compact.budget import truncate_tool_results
from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.events.bus import EventBus
from kama_claude.core.images import (
    MAX_IMAGE_BYTES,
    ImageError,
    ImageStore,
    check_image_budget,
)
from kama_claude.core.llm.images import ImageProvider
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.mcp.client import McpCallResult, McpClient, McpToolDef
from kama_claude.core.mcp.tool import McpTool
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.paths import PathAccessController
from kama_claude.core.tools.builtin.read_image import ReadImageParams, ReadImageTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.trace.provider import TracingProvider


# 生成真实可解码的小图片，避免仅检验伪造扩展名
def picture_bytes(fmt: str = "PNG", color: str = "red") -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(stream, format=fmt)
    return stream.getvalue()


# 功能：四类图片均能校验、去重、展开，并且上下文中没有编码内容
# 设计：使用真实 Pillow 图片并逐字节比较模型请求，检查原消息未被就地修改
@pytest.mark.parametrize("fmt,mime", [
    ("PNG", "image/png"), ("JPEG", "image/jpeg"),
    ("WEBP", "image/webp"), ("GIF", "image/gif"),
])
def test_formats_deduplicate_and_materialize(tmp_path: Path, fmt: str, mime: str) -> None:
    store = ImageStore(tmp_path / "images")
    data = picture_bytes(fmt)
    ref = store.put(data, source="中文 image.png", mime_type=mime)
    assert store.put(data).image_id == ref.image_id
    assert len(list(store.directory.glob("*.bin"))) == 1
    messages = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": [ref.model_dump()]},
    ]}]
    result = store.materialize(messages)
    block = result[0]["content"][0]["content"][0]
    assert block["type"] == "image"
    assert block["source"]["media_type"] == mime
    assert base64.b64decode(block["source"]["data"]) == data
    assert "base64" not in json.dumps(messages)
    assert (ref.width, ref.height) == (32, 24)


# 功能：动画图片只把首帧发送给模型
# 设计：生成红蓝两帧 GIF，读取缓存验证只有第一帧且颜色没有变成第二帧
def test_animation_uses_first_frame(tmp_path: Path) -> None:
    stream = io.BytesIO()
    Image.new("RGB", (12, 12), "red").save(
        stream, format="GIF", save_all=True, append_images=[Image.new("RGB", (12, 12), "blue")],
    )
    store = ImageStore(tmp_path / "images")
    ref = store.put(stream.getvalue(), mime_type="image/gif")
    _, data = store.load(ref.image_id)
    with Image.open(io.BytesIO(data)) as image:
        assert getattr(image, "n_frames", 1) == 1
        assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
    assert ref.mime_type == "image/png"


# 功能：非法内容、超大文件、伪造 MIME 与不支持的格式不会进入缓存
# 设计：覆盖不同失败入口并确认失败不留下任何图片文件
@pytest.mark.parametrize("case", ["empty", "broken", "large", "mime", "bmp", "base64"])
def test_invalid_images_rejected(tmp_path: Path, case: str) -> None:
    store = ImageStore(tmp_path / "images")
    with pytest.raises(ImageError):
        if case == "base64":
            store.put_base64("not valid $$$", "image/png", source="MCP")
        elif case == "mime":
            store.put(picture_bytes(), mime_type="image/jpeg")
        else:
            data = {"empty": b"", "broken": b"not an image", "large": b"x" * (MAX_IMAGE_BYTES + 1),
                    "bmp": picture_bytes("BMP")}[case]
            store.put(data)
    assert not list(tmp_path.rglob("*.bin"))


# 功能：读取参数必须恰好提供路径或图片 ID
# 设计：直接校验空参数、双参数与无效 ID，防止错误参数绕过路径审批
@pytest.mark.parametrize("params", [{}, {"path": "a", "image_id": "a" * 64},
                                    {"image_id": "../elsewhere"}, {"path": ""}])
def test_image_parameters_validate(params: dict) -> None:
    with pytest.raises(ValidationError):
        ReadImageParams.model_validate(params)


# 功能：不同会话不能凭图片 ID 访问另一个会话的缓存
# 设计：缓存只写入一个会话，另一个会话与目录穿越 ID 都必须失败
def test_cache_isolation_and_damage(tmp_path: Path) -> None:
    first = ImageStore(tmp_path / "first")
    other = ImageStore(tmp_path / "other")
    ref = first.put(picture_bytes())
    for image_id in [ref.image_id, "../first/" + ref.image_id]:
        with pytest.raises(ImageError):
            other.load(image_id)
    (first.directory / f"{ref.image_id}.bin").write_bytes(b"corrupt")
    with pytest.raises(ImageError, match="missing or damaged"):
        first.load(ref.image_id)


# 功能：路径读取支持中文、空格与相对路径，原图删除后缓存仍可使用
# 设计：经过真实工具调用与权限控制后删除原图，检查按 ID 回读的快照
async def test_read_path_and_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "中文 image.png"
    data = picture_bytes()
    source.write_bytes(data)
    store = ImageStore(tmp_path / "cache")
    tool = ReadImageTool(store, PathAccessController(tmp_path))
    result = await tool.invoke({"path": source.name})
    assert not result.is_error
    ref = result.content_blocks[-1]
    source.unlink()
    restored = await tool.invoke({"image_id": ref["image_id"]})
    assert not restored.is_error
    assert store.load(ref["image_id"])[1] == data


# 功能：外部图片遵循精确路径审批，拒绝时不会生成快照
# 设计：真实权限管理器分别同意与拒绝，并验证 read_image 的审批类型和读取结果
@pytest.mark.parametrize("decision", ["allow_once", "deny_once"])
async def test_image_path_permissions(tmp_path: Path, decision: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "outside.png"
    source.write_bytes(picture_bytes())
    store = ImageStore(workspace / "cache")
    tools = ToolRegistry()
    tools.register(ReadImageTool(store, PathAccessController(workspace)))
    bus = EventBus()
    manager = PermissionManager(timeout_s=1)
    requests = []

    # 接收真实审批事件并对精确请求作出决定
    async def respond(event) -> None:
        if isinstance(event, PermissionRequestedEvent):
            requests.append(event)
            manager.respond(event.tool_use_id, decision, event.request_id)

    bus.subscribe(respond)
    result = await invoke_tool(
        tools, ToolCallBlock("read", "read_image", {"path": str(source)}), bus, "r",
        permission_manager=manager, session_id="s",
    )
    assert len(requests) == 1
    assert requests[0].paths[0].operation == "read"
    assert requests[0].paths[0].path == str(source)
    assert result.is_error == (decision == "deny_once")
    assert bool(list(workspace.rglob("*.bin"))) == (decision == "allow_once")


# 功能：图片预算同时约束图片数量及累计字节，拒绝结果可恢复且不污染上下文
# 设计：使用合法元数据模拟字节边界，并通过调用编排验证失败不会返回图片块
async def test_budget_is_recoverable(tmp_path: Path) -> None:
    store = ImageStore(tmp_path / "cache")
    ref = store.put(picture_bytes())
    history = [{"role": "user", "content": [ref.model_dump()] * 20}]
    check_image_budget(history)
    tools = ToolRegistry()
    tools.register(ReadImageTool(store, PathAccessController(tmp_path)))
    result = await invoke_tool(
        tools, ToolCallBlock("read", "read_image", {"image_id": ref.image_id}),
        EventBus(), "r", image_history=history,
    )
    assert result.error_type == "image_error"
    assert "/compact" in result.content
    assert result.content_blocks is None
    large_ref = {**ref.model_dump(), "size": MAX_IMAGE_BYTES}
    check_image_budget([{"content": [large_ref] * 3}])
    with pytest.raises(ImageError):
        check_image_budget([{"content": [large_ref] * 4}])


# 功能：MCP 图文顺序、内嵌资源及 isError 均保留，工具失败不重复执行
# 设计：让同一结果同时包含两种图片及错误标记，经真实调用层验证结果和调用次数
async def test_mcp_mixed_content_and_error(tmp_path: Path) -> None:
    encoded = base64.b64encode(picture_bytes()).decode()
    client = McpClient()
    client._call = AsyncMock(return_value={"isError": True, "content": [
        {"type": "text", "text": "before"},
        {"type": "image", "data": encoded, "mimeType": "image/png"},
        {"type": "text", "text": "between"},
        {"type": "resource", "resource": {"uri": "image://result", "mimeType": "image/png",
                                           "blob": encoded}},
        {"type": "text", "text": "after"},
    ]})
    tool = McpTool(client, "server", McpToolDef("analyze", "image"), ImageStore(tmp_path / "cache"))
    tools = ToolRegistry()
    tools.register(tool)
    result = await invoke_tool(tools, ToolCallBlock("t", tool.name, {"path": "unchanged.png"}),
                               EventBus(), "r")
    assert result.is_error
    assert result.error_type == "mcp_tool_error"
    assert [b["text"] for b in result.content_blocks if b["type"] == "text"][::2] == [
        "before", "between", "after",
    ]
    assert sum(b["type"] == "image_ref" for b in result.content_blocks) == 2
    client._call.assert_awaited_once_with("tools/call", {
        "name": "analyze", "arguments": {"path": "unchanged.png"},
    })
    assert encoded not in result.content


# 功能：无效 MCP 图片不会导致工具重放，共享连接不会共享图片缓存
# 设计：先返回非法图片检查调用次数，再绑定两个缓存验证实例和目录隔离
async def test_mcp_invalid_and_bound_stores(tmp_path: Path) -> None:
    client = Mock(call_tool=AsyncMock(return_value=McpCallResult(content=[
        {"type": "image", "data": "@@@", "mimeType": "image/png"},
    ])))
    shared = McpTool(client, "server", McpToolDef("draw", "image"))
    first_store, second_store = ImageStore(tmp_path / "a"), ImageStore(tmp_path / "b")
    first, second = shared.with_images(first_store), shared.with_images(second_store)
    tools = ToolRegistry()
    tools.register(first)
    result = await invoke_tool(tools, ToolCallBlock("t", first.name, {}), EventBus(), "r")
    assert result.error_type == "image_error"
    client.call_tool.assert_awaited_once()
    assert shared._images is None
    assert second._images is second_store
    assert first._client is second._client


# 功能：压缩请求真实包含图片，摘要稳定保留缓存 ID，日志中没有图片编码
# 设计：组合真实压缩器与图片适配器，并比较 trace 看到的引用与底层模型收到的图片
async def test_compaction_and_trace(tmp_path: Path) -> None:
    store = ImageStore(tmp_path / "cache")
    ref = store.put(picture_bytes(), source="chart.png")
    messages = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": [
            {"type": "text", "text": "long" * 3000}, ref.model_dump(),
        ]},
    ]}]
    truncated = truncate_tool_results(messages)
    assert truncated[0]["content"][0]["content"][1] == ref.model_dump()
    assert len(truncated[0]["content"][0]["content"][0]["text"]) < 5000
    inner = Mock(chat=AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="red chart")))
    trace = Mock()
    provider = TracingProvider(ImageProvider(inner, store), trace)
    result = await Compactor(EventBus(), tmp_path, "s").compact_messages(truncated, provider)
    assert ref.image_id in result.summary_text
    assert "chart.png" in result.summary_text
    assert "red chart" in result.summary_text
    assert inner.chat.call_args.kwargs["messages"][0]["content"][-1]["type"] == "image"
    assert "base64" not in str(trace.emit.call_args_list)
    assert "image_ref" in str(trace.emit.call_args_list)


# 功能：只有明确的视觉不支持错误被转换为可理解提示
# 设计：用真实 SDK 异常区分模型能力错误与普通请求参数错误
@pytest.mark.parametrize("message,converted", [
    ("This model does not support images", True), ("Invalid tool schema", False),
])
async def test_provider_capability_error(tmp_path: Path, message: str, converted: bool) -> None:
    store = ImageStore(tmp_path / "cache")
    ref = store.put(picture_bytes())
    error = anthropic.BadRequestError(message, response=httpx.Response(
        400, request=httpx.Request("POST", "https://example.invalid/messages")), body={},
    )
    inner = Mock(chat=AsyncMock(side_effect=error))
    with pytest.raises(ImageError if converted else anthropic.BadRequestError):
        await ImageProvider(inner, store).chat(
            [{"role": "user", "content": [ref.model_dump()]}], [], EventBus(), "r",
        )


# 功能：缺失文件走稳定的文件错误分支，不误报模型识图成功
# 设计：通过真实工具调用层捕获文件系统异常并确认没有图片引用
async def test_missing_file(tmp_path: Path) -> None:
    tools = ToolRegistry()
    tools.register(ReadImageTool(ImageStore(tmp_path / "cache"), PathAccessController(tmp_path)))
    result = await invoke_tool(tools, ToolCallBlock("t", "read_image", {"path": "missing.png"}),
                               EventBus(), "r")
    assert result.is_error
    assert result.content_blocks is None
