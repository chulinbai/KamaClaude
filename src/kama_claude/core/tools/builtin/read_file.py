from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kama_claude.core.permissions.paths import PathAccessController
from kama_claude.core.tools.base import BaseTool, ToolResult

_MAX_BYTES = 512 * 1024  # 512 KB


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str


class ReadFileTool(BaseTool):
    path_access: PathAccessController
    params_model = ReadFileParams
    name = "read_file"
    description = (
        "Read the text content of a file. "
        "Accepts relative or absolute paths; paths outside allowed roots need user approval. "
        "Files larger than 512 KB are truncated."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path, relative to the workspace or absolute.",
            }
        },
        "required": ["path"],
    }

    # 注入统一文件系统权限，直接调用也不能绕过路径边界
    def __init__(self, access: PathAccessController | None = None) -> None:
        self.path_access = access or PathAccessController()

    # 在获准范围内读取文件内容，超 512KB 截断
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        path_str = ReadFileParams.model_validate(params).path

        path = Path(path_str)
        raw = self.path_access.read(path, _MAX_BYTES + 1)
        truncated = len(raw) > _MAX_BYTES
        text = raw[:_MAX_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[truncated]"

        return ToolResult(content=text)
