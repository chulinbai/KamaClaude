from __future__ import annotations

from typing import Any

from kama_claude.core.images import ImageError, ImageStore
from kama_claude.core.mcp.client import (
    McpClient,
    McpServerUnavailableError,
    McpToolDef,
    McpToolError,
)
from kama_claude.core.tools.base import BaseTool, ToolResult


# 将 MCP 工具包装为 BaseTool，使 ToolRegistry 可透明调用
class McpTool(BaseTool):
    params_model = None  # input_schema 来自 MCP tool_def，不使用 pydantic model
    retry_errors = False  # 返回内容失败不能重放可能有副作用的 MCP 调用

    # 初始化 MCP 工具包装器，工具名以 server_name__ 为前缀防止命名冲突
    def __init__(
        self, client: McpClient, server_name: str, tool_def: McpToolDef,
        images: ImageStore | None = None,
    ) -> None:
        self._client = client
        self._server_name = server_name
        self._tool_def = tool_def
        self._images = images
        self.name = f"{server_name}__{tool_def.name}"
        self.description = tool_def.description or f"MCP tool from {server_name}"
        self.input_schema: dict[str, Any] = (
            tool_def.input_schema or {"type": "object", "properties": {}}
        )

    # 为本轮生成独立包装器，避免把会话图片缓存写入共享 MCP 实例
    def with_images(self, images: ImageStore) -> McpTool:
        return McpTool(self._client, self._server_name, self._tool_def, images)

    # 调用 MCP server 上的工具，连接不可用或工具执行失败时返回 is_error=True
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        try:
            response = await self._client.call_tool(self._tool_def.name, dict(params))
            blocks: list[dict[str, Any]] = []
            summaries: list[str] = []
            for item in response.content:
                kind = item.get("type")
                if kind == "text":
                    text = str(item.get("text", ""))
                    blocks.append({"type": "text", "text": text})
                    summaries.append(text)
                    continue
                resource = item.get("resource", {}) if kind == "resource" else {}
                if not isinstance(resource, dict):
                    raise ImageError("Invalid MCP resource")
                if kind == "image" or (kind == "resource" and
                                       str(resource.get("mimeType", "")).startswith("image/")):
                    if self._images is None:
                        raise ImageError("No session image store is available")
                    payload = item if kind == "image" else resource
                    ref = self._images.put_base64(
                        payload.get("data" if kind == "image" else "blob"),
                        payload.get("mimeType"), source=f"MCP {self.name}",
                    )
                    blocks.extend([{"type": "text", "text": ref.summary()}, ref.model_dump()])
                    summaries.append(ref.summary())
                else:
                    text = str(resource.get("text") or resource.get("uri")
                               or item.get("uri") or f"Unsupported MCP content: {kind}")
                    blocks.append({"type": "text", "text": text})
                    summaries.append(text)
            return ToolResult(
                content="\n".join(summaries), content_blocks=blocks or None,
                is_error=response.is_error,
                error_type="mcp_tool_error" if response.is_error else None,
            )
        except ImageError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="image_error")
        except McpServerUnavailableError as exc:
            return ToolResult(
                content=f"mcp server '{self._server_name}' unavailable: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        except McpToolError as exc:
            return ToolResult(
                content=f"mcp tool '{self.name}' error: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        except Exception as exc:
            return ToolResult(
                content=f"mcp tool '{self.name}' unexpected error: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
