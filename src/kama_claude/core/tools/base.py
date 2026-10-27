from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from kama_claude.core.permissions.paths import PathAccessController


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    # 通用错误，以及 Bash 的 command_failed、sandbox_denied 和 sandbox_* 初始化错误
    error_type: str | None = None
    content_blocks: list[dict[str, Any]] | None = None


class BaseTool(ABC):
    path_access: PathAccessController | None = None
    execution_overhead_s: float = 0
    retry_errors: bool = True
    managed_timeout: bool = False
    name: str
    description: str
    input_schema: dict[str, object]
    params_model: ClassVar[type[BaseModel] | None] = None

    # 执行工具调用，返回结果或错误
    @abstractmethod
    async def invoke(self, params: dict[str, object]) -> ToolResult: ...
