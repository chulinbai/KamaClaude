from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.permissions.paths import PathAccessController
from kama_claude.core.tools.base import BaseTool, ToolResult

_MAX_DEPTH = 4
_MAX_ENTRIES = 200


class ListDirParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = "."
    max_depth: int = Field(default=2, ge=1, le=_MAX_DEPTH)


class ListDirTool(BaseTool):
    path_access: PathAccessController
    params_model = ListDirParams
    name = "list_dir"
    description = (
        "List the contents of a directory as a tree. "
        "Accepts relative or absolute paths; external paths need user approval. "
        "Hidden entries (starting with .) are included. "
        f"Maximum depth is {_MAX_DEPTH}, maximum total entries is {_MAX_ENTRIES}."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path, relative or absolute (default '.').",
            },
            "max_depth": {
                "type": "integer",
                "description": f"How many levels deep to recurse (default 2, max {_MAX_DEPTH}).",
            },
        },
        "required": [],
    }

    # 注入统一文件系统权限，直接调用也不能绕过路径边界
    def __init__(self, access: PathAccessController | None = None) -> None:
        self.path_access = access or PathAccessController()

    # 以树状格式列出获准目录内容，深度和条数有上限
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = ListDirParams.model_validate(params)
        path_str = p.path
        max_depth = p.max_depth

        root = self.path_access.check(Path(path_str), "read", "directory")
        if not root.exists():
            raise FileNotFoundError(f"no such directory: {path_str}")
        if not root.is_dir():
            raise NotADirectoryError(f"not a directory: {path_str}")

        lines: list[str] = [str(root) + "/"]
        count = 0

        # 递归枚举获准且不是链接的条目
        def _walk(directory: Path, depth: int, prefix: str) -> None:
            nonlocal count
            if depth > max_depth or count >= _MAX_ENTRIES:
                return
            entries = self.path_access.entries(directory)
            for i, (entry, is_directory) in enumerate(entries):
                if count >= _MAX_ENTRIES:
                    lines.append(f"{prefix}... (truncated)")
                    return
                connector = "└── " if i == len(entries) - 1 else "├── "
                suffix = "/" if is_directory else ""
                lines.append(f"{prefix}{connector}{entry.name}{suffix}")
                count += 1
                if is_directory and depth < max_depth:
                    extension = "    " if i == len(entries) - 1 else "│   "
                    _walk(entry, depth + 1, prefix + extension)

        _walk(root, 1, "")
        return ToolResult(content="\n".join(lines))
