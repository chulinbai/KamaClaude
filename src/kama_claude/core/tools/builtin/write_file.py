from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kama_claude.core.permissions.paths import PathAccessController
from kama_claude.core.tools.base import BaseTool, ToolResult

_MAX_BYTES = 1 * 1024 * 1024  # 1 MB


class WriteFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    content: str


class WriteFileTool(BaseTool):
    path_access: PathAccessController
    params_model = WriteFileParams
    name = "write_file"
    description = (
        "Write text content to a file, creating it if it "
        "does not exist, or overwriting it if it does. "
        "Accepts relative or absolute paths; external paths need user approval. "
        "Missing parents may only be created within an allowed directory; "
        "otherwise use create_dir. "
        "Content size is limited to 1 MB."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path, relative to the workspace or absolute.",
            },
            "content": {
                "type": "string",
                "description": "Text content to write.",
            },
        },
        "required": ["path", "content"],
    }

    # 注入统一文件系统权限，直接调用也不能绕过路径边界
    def __init__(self, access: PathAccessController | None = None) -> None:
        self.path_access = access or PathAccessController()

    # 写入获准文件，父目录只在已授权范围内创建
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = WriteFileParams.model_validate(params)
        path_str = p.path
        content = p.content

        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            return ToolResult(
                content=f"content too large: {len(encoded)} bytes (limit 1 MB)",
                is_error=True,
                error_type="runtime_error",
            )

        path = Path(path_str)
        self.path_access.write(path, encoded)

        return ToolResult(content=f"wrote {len(encoded)} bytes to {path_str}")
