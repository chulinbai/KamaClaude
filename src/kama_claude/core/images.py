from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import uuid
import warnings
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REQUEST_IMAGE_BYTES = 15 * 1024 * 1024
MAX_REQUEST_IMAGES = 20
_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


class ImageError(ValueError):
    pass


class ImageReference(BaseModel):
    type: Literal["image_ref"] = "image_ref"
    image_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    mime_type: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    size: int = Field(gt=0, le=MAX_IMAGE_BYTES)
    source: str = ""

    # 生成可展示给用户及模型的图片摘要
    def summary(self) -> str:
        return (f"Image {self.image_id}: {self.source or 'cached image'} "
                f"({self.mime_type}, {self.width}×{self.height}, {self.size} bytes)")


# 只遍历消息内容与工具结果，不将工具参数误当成图片引用
def image_references(messages: list[dict[str, Any]]) -> Iterator[ImageReference]:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image_ref":
                yield ImageReference.model_validate(block)
            elif block.get("type") == "tool_result":
                yield from image_references([block])


# 在图片进入上下文前检查累计预算，保留既有对话以便用户压缩后继续
def check_image_budget(messages: list[dict[str, Any]]) -> None:
    refs = list(image_references(messages))
    if len(refs) > MAX_REQUEST_IMAGES or sum(ref.size for ref in refs) > MAX_REQUEST_IMAGE_BYTES:
        raise ImageError(
            "Image request limit exceeded (20 images / 15 MiB before base64). "
            "Use /compact or smaller images, then read_image again."
        )


class ImageStore:
    # 将图片缓存固定在所属会话或独立运行目录内
    def __init__(self, directory: Path) -> None:
        self.directory = directory.parent.resolve() / directory.name
        if self.directory.resolve() != self.directory:
            raise ImageError("Image cache directory must not be a symbolic link or junction.")

    # 校验缓存文件名及实际路径，禁止引用其他目录或会话
    def _path(self, image_id: str, suffix: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", image_id):
            raise ImageError("Invalid image_id")
        path = self.directory / f"{image_id}.{suffix}"
        if path.resolve().parent != self.directory:
            raise ImageError("Image cache path is outside this session")
        return path

    # 原子写入缓存，避免并发读取半个文件
    def _write(self, path: Path, data: bytes) -> None:
        temporary = self.directory / f".{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    # 校验真实图片数据并保存模型实际使用的首帧快照
    def put(self, data: bytes, *, source: str = "", mime_type: str | None = None) -> ImageReference:
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ImageError("Image must be non-empty and at most 5 MiB; use a smaller image.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as picture:
                    actual_mime = _FORMATS.get(picture.format or "")
                    if actual_mime is None:
                        raise ImageError("Unsupported image format; use PNG, JPEG, WebP or GIF.")
                    if mime_type is not None and actual_mime != mime_type:
                        raise ImageError("Image MIME type does not match its actual format.")
                    picture.seek(0)
                    picture.load()
                    width, height = picture.size
                    if getattr(picture, "is_animated", False):
                        output = io.BytesIO()
                        picture.convert("RGBA").save(output, format="PNG")
                        data = output.getvalue()
                        actual_mime = "image/png"
                with Image.open(io.BytesIO(data)) as verified:
                    verified.verify()
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
                Image.DecompressionBombWarning) as exc:
            raise ImageError("Invalid, damaged or excessively large image.") from exc
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageError("The decoded first frame exceeds 5 MiB; use a smaller image.")
        ref = ImageReference(
            image_id=hashlib.sha256(data).hexdigest(), mime_type=actual_mime,
            width=width, height=height, size=len(data), source=source,
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        self._write(self._path(ref.image_id, "bin"), data)
        self._write(self._path(ref.image_id, "json"), ref.model_dump_json().encode())
        return ref

    # 先限制编码长度再严格解码 MCP 图片，避免无限制分配内存
    def put_base64(self, data: object, mime_type: object, *, source: str) -> ImageReference:
        if not isinstance(data, str) or len(data) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise ImageError("Invalid or oversized base64 image (maximum 5 MiB decoded).")
        if mime_type not in _FORMATS.values():
            raise ImageError("Unsupported image MIME type.")
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError as exc:
            raise ImageError("Invalid base64 image.") from exc
        return self.put(raw, source=source, mime_type=str(mime_type))

    # 校验图片元数据和内容哈希，缓存缺失或损坏时明确报告
    def load(self, image_id: str) -> tuple[ImageReference, bytes]:
        try:
            metadata = self._path(image_id, "json")
            if metadata.stat().st_size > 64 * 1024:
                raise ImageError("Image cache metadata is invalid.")
            ref = ImageReference.model_validate(json.loads(metadata.read_text(encoding="utf-8")))
            with self._path(image_id, "bin").open("rb") as stream:
                data = stream.read(MAX_IMAGE_BYTES + 1)
            if (ref.image_id != image_id or ref.size != len(data)
                    or hashlib.sha256(data).hexdigest() != image_id):
                raise ImageError("Image cache integrity check failed.")
            return ref, data
        except (OSError, ValueError) as exc:
            raise ImageError(
                f"Cached image {image_id} is missing or damaged; read the original path again."
            ) from exc

    # 在模型调用边界展开引用，不修改可持久化的原始消息
    def materialize(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        check_image_budget(messages)
        result = deepcopy(messages)
        cache: dict[str, tuple[ImageReference, bytes]] = {}
        total_bytes = 0

        # 仅转换内容块，忽略工具参数和其他普通对象
        def convert(items: list[dict[str, Any]]) -> None:
            nonlocal total_bytes
            for item in items:
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if block.get("type") == "image_ref":
                        image_id = str(block["image_id"])
                        if image_id not in cache:
                            cache[image_id] = self.load(image_id)
                        ref, data = cache[image_id]
                        total_bytes += len(data)
                        if total_bytes > MAX_REQUEST_IMAGE_BYTES:
                            raise ImageError("Image request exceeds 15 MiB; use /compact.")
                        block.clear()
                        block.update({"type": "image", "source": {
                            "type": "base64", "media_type": ref.mime_type,
                            "data": base64.b64encode(data).decode("ascii"),
                        }})
                    elif block.get("type") == "tool_result":
                        convert([block])

        convert(result)
        return result
