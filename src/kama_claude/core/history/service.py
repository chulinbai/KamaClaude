from __future__ import annotations

import asyncio
import heapq
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from kama_claude.core.history.archive import HistoryArchive, parse_record_id
from kama_claude.core.history.index import HistoryIndex, SearchStopped, check_stop
from kama_claude.core.history.models import (
    HistoryHit,
    HistoryReadParams,
    HistoryReadResult,
    HistoryRecord,
    HistorySearchParams,
    HistorySearchResult,
    content_text,
    normalize,
    record_text,
    utc_time,
)
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore

T = TypeVar("T")
Scope = frozenset[str | None]


# 工作区只按规范化的绝对路径匹配，不自动扩大到仓库根目录
def workspace_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


@dataclass
class SourceSnapshot:
    session: Session
    archive: HistoryArchive
    manifest: dict[str, Any]


class HistoryService:
    # 共用归档存储、可丢弃索引和可协作取消的后台构建任务
    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.config = store.history_config
        self.index = HistoryIndex(store.root)
        self._build_task: asyncio.Task[None] | None = None
        self._build_stop = threading.Event()
        self._pending: dict[str, tuple[HistoryArchive, dict[str, Any]]] = {}
        self._rebuild = False
        self._closing = False
        self._query_stops: set[threading.Event] = set()
        self._query_tasks: set[asyncio.Task[Any]] = set()

    # 仅返回会话元数据中的工作区集合，读取正文前用于范围审批
    def workspaces(self) -> Scope:
        return frozenset(
            workspace_key(session.workspace) if session.workspace else None
            for session in self.store.list_meta()
        )

    # 按服务器授予的范围过滤会话，scope=all 本身不产生授权
    def _sessions(
        self, workspace: Path, scope: str, allowed: Scope | None,
        target_session_id: str | None = None,
        check: Callable[[], None] | None = None,
    ) -> list[Session]:
        current = workspace_key(workspace)
        if scope == "all" and allowed is None:
            raise PermissionError("Cross-workspace history authorization is required")
        permitted = frozenset({current}) if scope == "workspace" else allowed
        assert permitted is not None
        return [
            session for session in self.store.list_meta(check)
            if (target_session_id is None or session.id == target_session_id)
            and (workspace_key(session.workspace) if session.workspace else None) in permitted
        ]

    # 获取查询开始时的完整记录边界，归档异常使调用方显式报告覆盖缺口
    def _sources(
        self, sessions: list[Session], stop: threading.Event, deadline: float,
        warnings: list[str],
    ) -> list[SourceSnapshot]:
        sources = []
        for session in sessions:
            check_stop(stop, deadline)
            try:
                archive = self.store.history(session.id, lambda: check_stop(stop, deadline))
                manifest = archive.snapshot(lambda: check_stop(stop, deadline))
                warnings.extend(f"{session.id}: {item}" for item in manifest["warnings"])
                sources.append(SourceSnapshot(session, archive, manifest))
            except (OSError, ValueError) as exc:
                warnings.append(f"{session.id}: {exc}")
        return sources

    # 将查询取消传递至实际线程，等待线程退出后才结束取消流程
    async def _worker(self, action: Callable[[threading.Event], T]) -> T:
        if self._closing:
            raise RuntimeError("History service is stopping")
        stop = threading.Event()
        self._query_stops.add(stop)
        task = asyncio.create_task(asyncio.to_thread(action, stop))
        self._query_tasks.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stop.set()
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            raise
        finally:
            self._query_stops.discard(stop)
            self._query_tasks.discard(task)

    # 将多个检索请求合并为共享后台构建，不占用单次搜索预算
    def _schedule_build(
        self, sources: list[SourceSnapshot], rebuild: bool = False,
    ) -> None:
        if self._closing:
            return
        self._pending.update({s.session.id: (s.archive, s.manifest) for s in sources})
        self._rebuild = self._rebuild or rebuild
        if self._build_task is None or self._build_task.done():
            self._build_stop.clear()
            self._build_task = asyncio.create_task(self._build())

    # 按快照维护索引并保留构建错误，避免后台异常丢失
    async def _build(self) -> None:
        while self._pending and not self._closing:
            sources = list(self._pending.values())
            self._pending.clear()
            rebuild, self._rebuild = self._rebuild, False
            try:
                await asyncio.to_thread(
                    self.index.build, sources, self._build_stop, rebuild=rebuild,
                )
            except SearchStopped:
                return
            except (OSError, ValueError, sqlite3.Error) as exc:
                if not self._build_stop.is_set():
                    self.index.error = str(exc)
                return

    # 关闭时停止实际查询与索引线程，正常消息不依赖索引成功
    async def close(self) -> None:
        self._closing = True
        for stop in self._query_stops:
            stop.set()
        self._build_stop.set()
        if self._query_tasks:
            await asyncio.gather(*self._query_tasks, return_exceptions=True)
        if self._build_task is not None:
            await self._build_task

    # 对扫描和索引记录使用相同的时间过滤与稳定排序
    def _rank(
        self, record: HistoryRecord, text: str, query: str, terms: list[str],
        params: HistorySearchParams,
    ) -> tuple[int, int, int, float, str] | None:
        if not text or not all(term in text for term in terms):
            return None
        try:
            stamp = utc_time(datetime.fromisoformat(record.timestamp))
        except ValueError:
            stamp = datetime.min.replace(tzinfo=UTC)
        if params.since and stamp < utc_time(params.since):
            return None
        if params.until and stamp > utc_time(params.until):
            return None
        seconds = (stamp - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()
        return (
            int(record.kind not in {"compaction_summary", "note_snapshot"}),
            int(query in text), sum(min(text.count(term), 3) for term in terms),
            seconds, record.record_id,
        )

    # 构造带时间和出处的摘录，不将历史内容提升为系统指令
    def _hit(self, source: SourceSnapshot, record: HistoryRecord, terms: list[str]) -> HistoryHit:
        text = record_text(record)
        # 规范化可能改变字符长度，保守以原文索引定位并保留有限摘录。
        positions = [text.casefold().find(term) for term in terms]
        found = [position for position in positions if position >= 0]
        start = max(0, min(found) - 120) if found else 0
        return HistoryHit(
            record_id=record.record_id, session_id=source.session.id,
            title=source.session.title, workspace=source.session.workspace,
            run_id=record.run_id, sequence=record.sequence, timestamp=record.timestamp,
            kind=record.kind, excerpt=text[start:start + 500], source=record.source,
        )

    # 异步提供统一查询，搜索开始时固定源文件边界
    async def search(
        self, params: HistorySearchParams, workspace: Path, allowed: Scope | None = None,
    ) -> HistorySearchResult:
        query = normalize(params.query.strip())
        if not query:
            raise ValueError("History query must not be blank")
        if params.since and params.until and utc_time(params.since) > utc_time(params.until):
            raise ValueError("since must not be later than until")
        loop = asyncio.get_running_loop()
        deadline = time.perf_counter() + self.config.search_timeout_seconds
        return await self._worker(lambda stop: self._search(
            params, workspace, allowed, query, stop, deadline, loop,
        ))

    # 流式维护 Top-K，只对已授权快照执行扫描或索引候选校验
    def _search(
        self, params: HistorySearchParams, workspace: Path, allowed: Scope | None,
        query: str, stop: threading.Event, deadline: float, loop: asyncio.AbstractEventLoop,
    ) -> HistorySearchResult:
        terms = list(dict.fromkeys(query.split()))
        result = HistorySearchResult()
        limit = params.limit or self.config.max_results
        heap: list[tuple[tuple[int, int, int, float, str], str, HistoryHit]] = []
        matched = 0

        # 校验候选并维护有界结果集合，相同身份不占用两个名额
        def consider(source: SourceSnapshot, record: HistoryRecord) -> None:
            nonlocal matched
            rank = self._rank(record, normalize(record_text(record)), query, terms, params)
            if rank is None:
                return
            matched += 1
            if any(item[1] == record.record_id for item in heap):
                return
            if len(heap) < limit or rank > heap[0][0]:
                item = (rank, record.record_id, self._hit(source, record, terms))
                if len(heap) < limit:
                    heapq.heappush(heap, item)
                else:
                    heapq.heapreplace(heap, item)

        try:
            sessions = self._sessions(
                workspace, params.scope, allowed, params.target_session_id,
                lambda: check_stop(stop, deadline),
            )
            result.total_candidate_sessions = len(sessions)
            sources = self._sources(sessions, stop, deadline, result.warnings)
            build_sources = [SourceSnapshot(s.session, s.archive, deepcopy(s.manifest))
                             for s in sources]
            since = utc_time(params.since).isoformat() if params.since else None
            until = utc_time(params.until).isoformat() if params.until else None
            for source in sources:
                source.manifest["segments"] = [
                    segment for segment in source.manifest["segments"]
                    if segment.get("unknown_time", True) or (
                        (since is None or segment["max_time"] >= since)
                        and (until is None or segment["min_time"] <= until)
                    )
                ]
            size = sum(s["text_bytes"] for source in sources for s in source.manifest["segments"])
            indexed = self.config.backend == "sqlite" or (
                self.config.backend == "auto" and size >= self.config.index_threshold_bytes
            )
            available = indexed and self.index.available(stop, deadline)
            if indexed and not available and sources:
                loop.call_soon_threadsafe(self._schedule_build, build_sources)
            if available:
                result.backend = "sqlite"
            elif indexed and sources:
                result.reason = "index_building"
            for source in sources:
                coverage: dict[str, int] = {}
                if available:
                    try:
                        coverage = self.index.coverage(
                            source.archive, source.manifest, stop, deadline,
                        )
                        if any(coverage.get(segment["name"], 0) < segment["end"]
                               for segment in source.manifest["segments"]):
                            loop.call_soon_threadsafe(self._schedule_build, build_sources)
                        for row in self.index.candidates(
                            source.session.id, terms, coverage, stop, deadline,
                        ):
                            check_stop(stop, deadline)
                            with source.archive.path(row["segment"]).open("rb") as stream:
                                stream.seek(row["offset"])
                                record = HistoryRecord.model_validate_json(
                                    stream.read(row["end"] - row["offset"]),
                                )
                            if record.record_id != row["rid"]:
                                raise ValueError("Stale history index locator")
                            consider(source, record)
                    except (sqlite3.Error, OSError, ValueError) as exc:
                        check_stop(stop, deadline)
                        coverage = {}
                        self.index.error = str(exc)
                        result.warnings.append(f"index fallback: {exc}")
                        result.backend = "scan"
                        loop.call_soon_threadsafe(self._schedule_build, build_sources, True)
                for segment in source.manifest["segments"]:
                    check_stop(stop, deadline)
                    offset = coverage.get(segment["name"], 0)
                    if offset >= segment["end"]:
                        continue
                    try:
                        with source.archive.path(segment["name"]).open("rb") as stream:
                            stream.seek(offset)
                            while stream.tell() < segment["end"]:
                                check_stop(stop, deadline)
                                raw = stream.readline(segment["end"] - stream.tell())
                                if not raw or not raw.endswith(b"\n"):
                                    result.warnings.append("incomplete history tail")
                                    break
                                try:
                                    record = HistoryRecord.model_validate_json(raw)
                                    expected_id = f"{source.session.id}:{record.sequence}"
                                    if (record.session_id != source.session.id
                                            or record.record_id != expected_id):
                                        raise ValueError("Record session mismatch")
                                    consider(source, record)
                                except ValueError:
                                    result.warnings.append(f"invalid record: {segment['name']}")
                    except OSError as exc:
                        result.warnings.append(str(exc))
                result.searched_sessions += 1
        except SearchStopped:
            result.complete = False
            result.reason = "cancelled" if stop.is_set() else "search_timeout"
        if result.warnings:
            result.complete = False
            result.reason = result.reason or "source_warning"
            result.warnings = sorted(set(result.warnings))[:50]
        result.results = [item[2] for item in sorted(heap, reverse=True)]
        result.truncated = matched > len(result.results)
        return result

    # 按稳定记录身份重新授权并跨分片展开，输出分页不暴露存储路径输入
    async def read(
        self, params: HistoryReadParams, workspace: Path, allowed: Scope | None = None,
    ) -> HistoryReadResult:
        deadline = time.perf_counter() + self.config.search_timeout_seconds
        return await self._worker(
            lambda stop: self._read(params, workspace, allowed, stop, deadline),
        )

    # 从序号范围选取少量相邻记录，返回原文字符分页
    def _read(
        self, params: HistoryReadParams, workspace: Path, allowed: Scope | None,
        stop: threading.Event, deadline: float,
    ) -> HistoryReadResult:
        sid, sequence = parse_record_id(params.record_id)
        sessions = self._sessions(
            workspace, params.scope, allowed, sid, lambda: check_stop(stop, deadline),
        )
        if not sessions:
            raise PermissionError("History record is unavailable in the authorized scope")
        warnings: list[str] = []
        sources = self._sources(sessions, stop, deadline, warnings)
        if not sources:
            raise FileNotFoundError("History archive is unavailable")
        source = sources[0]
        source.manifest["segments"] = [
            s for s in source.manifest["segments"]
            if s["last_sequence"] >= sequence - params.before
            and s["first_sequence"] <= sequence + params.after
        ]
        records: dict[int, HistoryRecord] = {}
        for record, _ in source.archive.records(
            source.manifest, warnings, lambda: check_stop(stop, deadline),
        ):
            if sequence - params.before <= record.sequence <= sequence + params.after:
                records[record.sequence] = record
        if sequence not in records:
            raise FileNotFoundError("History record no longer exists")
        ordered = [records[key] for key in sorted(records)]
        body = "\n\n".join(
            f"[{record.record_id} | {record.timestamp} | {record.kind}]\n"
            + content_text(record.payload.get("content", "")) for record in ordered
        )
        end = params.offset + self.config.max_read_chars
        hits = [self._hit(source, record, []) for record in ordered]
        for hit in hits:
            hit.excerpt = ""
        return HistoryReadResult(
            records=hits, text=body[params.offset:end],
            next_offset=end if end < len(body) else None,
            complete=not warnings, warnings=warnings,
        )

    # 返回当前授权范围的分片和缓存状态，后台任务进度不需要运行模型
    async def status(
        self, workspace: Path, scope: str = "workspace", allowed: Scope | None = None,
        *, rebuild: bool = False,
    ) -> dict[str, Any]:
        warnings: list[str] = []
        sources = await self._worker(lambda stop: self._sources(
            self._sessions(workspace, scope, allowed,
                           check=lambda: check_stop(stop, float("inf"))),
            stop, float("inf"), warnings,
        ))
        if rebuild:
            self._schedule_build(sources, rebuild=True)
        building = self._build_task is not None and not self._build_task.done()
        segments = [segment for source in sources for segment in source.manifest["segments"]]
        available = await asyncio.to_thread(self.index.available)
        return {
            "state": "BUILDING_INDEX" if building else "INDEX_DEGRADED" if self.index.error
            else "INDEX_READY" if available else "SCAN",
            "sessions": len(sources), "segments": len(segments),
            "archive_bytes": sum(s["end"] for s in segments),
            "searchable_bytes": sum(s["text_bytes"] for s in segments),
            "oversized_segments": sum(s["end"] > self.config.segment_max_bytes for s in segments),
            "indexed_segments_processed": self.index.processed_segments,
            "index_build_segments": self.index.total_segments,
            "warnings": warnings, "index_error": self.index.error,
        }
