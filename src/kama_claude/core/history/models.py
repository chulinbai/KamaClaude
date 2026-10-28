from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

RecordKind = Literal[
    "user_message", "assistant_message", "tool_result", "note_snapshot", "compaction_summary",
]


class HistoryConfig(BaseModel):
    backend: Literal["auto", "scan", "sqlite"] = "auto"
    segment_max_bytes: int = Field(default=32 * 1024 * 1024, gt=0, strict=True)
    index_threshold_bytes: int = Field(default=100 * 1024 * 1024, gt=0, strict=True)
    search_timeout_seconds: float = Field(default=3.0, gt=0, allow_inf_nan=False)
    max_results: int = Field(default=10, ge=1, le=50, strict=True)
    max_read_chars: int = Field(default=12_000, ge=1, le=100_000, strict=True)


class HistoryRecord(BaseModel):
    schema_version: Literal[1] = 1
    record_id: str
    session_id: str
    run_id: str | None = None
    sequence: int = Field(gt=0, le=2**63 - 1)
    timestamp: str
    kind: RecordKind
    payload: dict[str, Any]
    source: dict[str, Any] = Field(default_factory=dict)


class ArchiveReceipt(BaseModel):
    record_id: str
    sequence: int
    segment: str
    offset: int
    end: int


class ArchiveCheckpoint(BaseModel):
    session_id: str
    sequence: int


class HistorySearchParams(BaseModel):
    query: str = Field(min_length=1, max_length=2048)
    scope: Literal["workspace", "all"] = "workspace"
    target_session_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int | None = Field(default=None, ge=1, le=50)


class HistoryReadParams(BaseModel):
    record_id: str = Field(min_length=1, max_length=200)
    scope: Literal["workspace", "all"] = "workspace"
    before: int = Field(default=2, ge=0, le=20)
    after: int = Field(default=2, ge=0, le=20)
    offset: int = Field(default=0, ge=0)


class HistoryHit(BaseModel):
    record_id: str
    session_id: str
    title: str
    workspace: str | None
    run_id: str | None
    sequence: int
    timestamp: str
    kind: RecordKind
    excerpt: str
    source: dict[str, Any] = Field(default_factory=dict)


class HistorySearchResult(BaseModel):
    results: list[HistoryHit] = Field(default_factory=list)
    backend: str = "scan"
    complete: bool = True
    truncated: bool = False
    reason: str | None = None
    searched_sessions: int = 0
    total_candidate_sessions: int = 0
    warnings: list[str] = Field(default_factory=list)


class HistoryReadResult(BaseModel):
    records: list[HistoryHit]
    text: str
    next_offset: int | None = None
    complete: bool = True
    warnings: list[str] = Field(default_factory=list)


class HistoryStatusResult(BaseModel):
    state: Literal["SCAN", "BUILDING_INDEX", "INDEX_READY", "INDEX_DEGRADED"]
    sessions: int
    segments: int
    archive_bytes: int
    searchable_bytes: int
    oversized_segments: int
    indexed_segments_processed: int
    index_build_segments: int
    warnings: list[str]
    index_error: str | None


# 将查询和文档转换为后端共享的 Unicode 子串匹配形式
def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


# 提取可搜索文本，过滤思考、图片编码和历史工具返回的递归摘录
def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "tool_use":
            if block.get("name") not in {"history_search", "history_read"}:
                parts.append(str(block.get("name", "")))
                parts.append(json.dumps(block.get("input", {}), ensure_ascii=False))
        elif kind == "tool_result":
            parts.append(content_text(block.get("content", "")))
    return "\n".join(parts)


# 生成原文检索投影，历史工具证据仍保存在归档但不再次索引
def record_text(record: HistoryRecord) -> str:
    if record.source.get("tool_name") in {"history_search", "history_read"}:
        return ""
    return content_text(record.payload.get("content", ""))


# 统一时间边界为带 UTC 时区的时间戳
def utc_time(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
