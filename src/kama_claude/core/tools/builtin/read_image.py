from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kama_claude.core.images import MAX_IMAGE_BYTES, ImageError, ImageStore
from kama_claude.core.permissions.paths import PathAccessController
from kama_claude.core.tools.base import BaseTool, ToolResult


class ReadImageParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str | None = Field(default=None, min_length=1)
    image_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    # 读取原文件与读取会话快照必须二选一
    def exactly_one_source(self) -> Self:
        if (self.path is None) == (self.image_id is None):
            raise ValueError("Provide exactly one of path or image_id")
        return self


class ReadImageTool(BaseTool):
    name = "read_image"
    description = (
        "View a local image to answer questions about it. Use path (absolute or relative to "
        "the workspace), or image_id to revisit a cached image from this session. "
        "Supports PNG, JPEG, WebP and GIF (first frame), maximum 5 MiB. "
        "Paths outside allowed roots require approval. Images are sent to the current model."
    )
    params_model = ReadImageParams
    input_schema = ReadImageParams.model_json_schema()
    retry_errors = False

    # 注入所属会话的图片缓存和现有路径权限
    def __init__(self, images: ImageStore, access: PathAccessController) -> None:
        self.images = images
        self.path_access = access

    # 读取获准图片或已有快照，返回文字摘要和可持久化引用
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        args = ReadImageParams.model_validate(params)
        try:
            if args.image_id is not None:
                ref, _ = self.images.load(args.image_id)
            else:
                assert args.path is not None
                assert self.path_access is not None
                target = self.path_access.check(Path(args.path), "read", "file")
                data = self.path_access.read(target, MAX_IMAGE_BYTES + 1)
                ref = self.images.put(data, source=str(target))
            return ToolResult(content=ref.summary(), content_blocks=[
                {"type": "text", "text": ref.summary()}, ref.model_dump(),
            ])
        except ImageError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="image_error")
