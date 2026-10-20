from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kama_claude.core.permissions.paths import PathAccessController
from kama_claude.core.tools.base import BaseTool, ToolResult


class CreateDirParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class CreateDirTool(BaseTool):
    path_access: PathAccessController
    params_model = CreateDirParams
    name = "create_dir"
    description = (
        "Create one exact directory with an existing parent. External targets require approval "
        "for that directory and its subtree, never its parent. Existing directories are unchanged. "
        "Prefer this tool to shell mkdir; after creation Shell can request the exact existing path."
    )
    input_schema: dict[str, object] = {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
    }

    # 注入统一路径权限，创建操作仅在宿主受控函数内进行
    def __init__(self, access: PathAccessController | None = None) -> None:
        self.path_access = access or PathAccessController()

    # 创建获准的精确目录，绝不自动授权或创建外部父目录
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        target = self.path_access.mkdir(Path(CreateDirParams.model_validate(params).path))
        return ToolResult(content=f"Directory ready: {target}")
