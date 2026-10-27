from __future__ import annotations

import anthropic

from kama_claude.core.events.bus import EventBus
from kama_claude.core.images import ImageError, ImageStore, image_references
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.types import LlmResponse


class ImageProvider:
    # 每轮绑定独立缓存，底层网络客户端可以跨会话共享
    def __init__(self, inner: LLMProvider, images: ImageStore) -> None:
        self.inner = inner
        self.images = images

    # 将内部图片引用展开为供应商图片块，错误中不包含请求体
    async def chat(
        self, messages: list[dict[str, object]], tool_schemas: list[dict[str, object]],
        bus: EventBus, run_id: str, *, step: int = 0, system: str | None = None,
    ) -> LlmResponse:
        has_images = any(image_references(messages))
        prepared = self.images.materialize(messages) if has_images else messages
        try:
            return await self.inner.chat(
                messages=prepared, tool_schemas=tool_schemas, bus=bus, run_id=run_id,
                step=step, system=system,
            )
        except anthropic.BadRequestError as exc:
            error = str(exc).lower()
            if has_images and ("image" in error or "vision" in error) and any(
                word in error for word in ("not support", "unsupported", "does not support")
            ):
                raise ImageError(
                    "当前模型或接口不支持图片输入，请切换支持视觉的模型后重试。"
                ) from None
            raise
