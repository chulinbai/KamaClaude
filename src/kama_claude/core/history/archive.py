from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from kama_claude.core.history.models import (
    ArchiveCheckpoint,
    ArchiveReceipt,
    HistoryRecord,
    RecordKind,
    record_text,
    utc_time,
)


class ArchiveError(OSError):
    pass


# 校验并拆解逻辑记录位置，拒绝将用户输入作为文件路径
def parse_record_id(value: str) -> tuple[str, int]:
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_-]{0,127}):([1-9][0-9]{0,18})", value)
    if match is None:
        raise ValueError("Invalid history record ID")
    sequence = int(match[2])
    if sequence > 2**63 - 1:
        raise ValueError("Invalid history record sequence")
    return match[1], sequence


class HistoryArchive:
    # 绑定单个会话的分片目录，构造时不创建或导入文件
    def __init__(self, session_dir: Path, segment_max_bytes: int = 32 * 1024 * 1024) -> None:
        self.session_dir = session_dir.resolve()
        self.sid = self.session_dir.name
        self.directory = self.session_dir / "history"
        self.segment_max_bytes = segment_max_bytes
        self._lock = threading.RLock()
        self._manifest: dict[str, Any] | None = None
        self._stamp: tuple[int, int] | None = None
        self._depth = 0

    # 解析归档内部的固定文件名并拒绝符号链接逃逸
    def path(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ArchiveError("Invalid archive filename")
        directory = self.directory.resolve()
        if directory.parent != self.session_dir:
            raise ArchiveError("Archive directory escapes session")
        path = (directory / name).resolve()
        if path.parent != directory:
            raise ArchiveError("Archive file escapes history directory")
        return path

    # 在进程内锁和跨进程文件锁下串行更新归档
    @contextmanager
    def transaction(self, check: Callable[[], None] | None = None) -> Iterator[None]:
        with self._thread_lock(check):
            if self._depth:
                yield
                return
            lock_path = self.path("writer.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as lock:
                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                try:
                    if sys.platform == "win32":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise ArchiveError("History archive is owned by another writer") from exc
                try:
                    self._depth = 1
                    self._load(check)
                    yield
                except BaseException:
                    self._manifest = None
                    raise
                finally:
                    self._depth = 0
                    lock.seek(0)
                    if sys.platform == "win32":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    # 等待同进程写入时继续检查预算和取消，避免查询卡在锁上
    @contextmanager
    def _thread_lock(self, check: Callable[[], None] | None) -> Iterator[None]:
        while not self._lock.acquire(timeout=0.05):
            if check is not None:
                check()
        try:
            if check is not None:
                check()
            yield
        finally:
            self._lock.release()

    # 返回文件变更指纹，缺失时返回空值
    def _stat(self, path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            return None

    # 恢复清单并检查活动尾行，清单始终可从实际记录重建
    def _load(self, check: Callable[[], None] | None = None) -> None:
        manifest_path = self.path("manifest.json")
        stamp = self._stat(manifest_path)
        if self._manifest is not None and stamp == self._stamp:
            segments = self._manifest["segments"]
            names = sorted(p.name for p in self.directory.glob("history-*.jsonl"))
            if names == [s["name"] for s in segments] and all(
                self._stat(self.path(s["name"])) == tuple(s["fingerprint"]) for s in segments
            ):
                return
        previous: dict[str, Any] = {}
        if stamp is not None:
            try:
                previous = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(previous, dict) or previous.get("schema_version") != 1:
                    previous = {}
            except (OSError, ValueError):
                pass
        names = sorted(
            p.name for p in self.directory.glob("history-*.jsonl")
            if re.fullmatch(r"history-[0-9]{6,}.jsonl", p.name)
        )
        try:
            if (
                previous.get("schema_version") == 1
                and previous.get("session_id") == self.sid
                and names == [segment["name"] for segment in previous["segments"]]
                and all(self._stat(self.path(segment["name"])) == tuple(segment["fingerprint"])
                        for segment in previous["segments"])
            ):
                self._manifest = previous
                self._stamp = stamp
                return
        except (KeyError, TypeError, ValueError):
            previous = {}
        segments = []
        warnings: list[str] = []
        native_records = False
        last_sequence = int(previous.get("last_sequence", 0))
        for index, name in enumerate(names):
            path = self.path(name)
            info: dict[str, Any] = {
                "name": name, "first_sequence": 0, "last_sequence": 0,
                "records": 0, "text_bytes": 0, "end": 0,
                "min_time": "", "max_time": "", "sealed": index < len(names) - 1,
            }
            with path.open("rb") as stream:
                while raw := stream.readline():
                    if check is not None:
                        check()
                    start = stream.tell() - len(raw)
                    if not raw.endswith(b"\n"):
                        warnings.append(f"incomplete tail: {name}:{start}")
                        if index == len(names) - 1:
                            recovery = self.path(f"recovery-{uuid.uuid4().hex}.bin")
                            with recovery.open("wb") as saved:
                                saved.write(raw)
                                saved.flush()
                                os.fsync(saved.fileno())
                        break
                    info["end"] = stream.tell()
                    try:
                        record = HistoryRecord.model_validate_json(raw)
                        if record.session_id != self.sid or record.record_id != (
                            f"{self.sid}:{record.sequence}"
                        ):
                            raise ValueError("Record identity mismatch")
                    except ValueError:
                        warnings.append(f"invalid record: {name}:{start}")
                        continue
                    self._add_info(info, record)
                    native_records = native_records or "import_key" not in record.source
                    last_sequence = max(last_sequence, record.sequence)
            if index == len(names) - 1 and path.stat().st_size > info["end"]:
                with path.open("r+b") as repaired:
                    repaired.truncate(info["end"])
                    repaired.flush()
                    os.fsync(repaired.fileno())
            info["fingerprint"] = list(self._stat(path) or (0, 0))
            info["sealed"] = info["sealed"] or info["end"] >= self.segment_max_bytes
            segments.append(info)
        missing = {s["name"] for s in previous.get("segments", [])} - set(names)
        warnings.extend(f"missing segment: {name}" for name in sorted(missing))
        self._manifest = {
            "schema_version": 1, "session_id": self.sid, "segments": segments,
            "last_sequence": last_sequence,
            "durable_sequence": min(int(previous.get("durable_sequence", 0)), last_sequence),
            "legacy_imported": previous.get("legacy_imported", False) or native_records,
            "warnings": sorted(set([*previous.get("warnings", []), *warnings])),
        }
        self._write_manifest()

    # 维护每个分片的可重建范围和检索体积统计
    def _add_info(self, info: dict[str, Any], record: HistoryRecord) -> None:
        if not info["first_sequence"]:
            info["first_sequence"] = record.sequence
        info["last_sequence"] = max(info["last_sequence"], record.sequence)
        info["records"] += 1
        info["text_bytes"] += len(record_text(record).encode("utf-8"))
        try:
            stamp = utc_time(datetime.fromisoformat(record.timestamp)).isoformat()
        except ValueError:
            info["unknown_time"] = True
            stamp = ""
        info.setdefault("unknown_time", False)
        if stamp:
            info["min_time"] = min(info["min_time"] or stamp, stamp)
            info["max_time"] = max(info["max_time"], stamp)

    # 原子发布清单，不依赖清单提交成功来保证原始记录存在
    def _write_manifest(self) -> None:
        assert self._manifest is not None
        temporary = self.path(f"manifest-{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(json.dumps(self._manifest, ensure_ascii=False).encode("utf-8"))
                stream.flush()
            for attempt in range(5):
                try:
                    temporary.replace(self.path("manifest.json"))
                    break
                except PermissionError:
                    if sys.platform != "win32" or attempt == 4:
                        raise
                    time.sleep(0.01 * (attempt + 1))
            self._stamp = self._stat(self.path("manifest.json"))
        finally:
            temporary.unlink(missing_ok=True)

    # 返回固定完整记录边界的查询快照，调用方不持有写入锁
    def snapshot(self, check: Callable[[], None] | None = None) -> dict[str, Any]:
        with self.transaction(check):
            return json.loads(json.dumps(self._manifest))  # type: ignore[no-any-return]

    # 以会话序号定位记录，重复提交时校验完整内容
    def _existing(self, sequence: int) -> tuple[HistoryRecord, ArchiveReceipt] | None:
        assert self._manifest is not None
        for segment in self._manifest["segments"]:
            if not segment["first_sequence"] <= sequence <= segment["last_sequence"]:
                continue
            with self.path(segment["name"]).open("rb") as stream:
                while stream.tell() < segment["end"]:
                    offset = stream.tell()
                    raw = stream.readline()
                    if not raw:
                        break
                    try:
                        record = HistoryRecord.model_validate_json(raw)
                    except ValueError:
                        continue
                    if record.sequence == sequence:
                        return record, ArchiveReceipt(
                            record_id=record.record_id, sequence=sequence,
                            segment=segment["name"], offset=offset, end=stream.tell(),
                        )
        return None

    # 追加一个带固定身份的完整记录，失败重试不会重复生成记录
    def append_record(self, record: HistoryRecord) -> ArchiveReceipt:
        with self.transaction():
            return self._append(record)

    # 在已持有写锁时执行幂等检查、滚动和追加
    def _append(self, record: HistoryRecord) -> ArchiveReceipt:
        assert self._manifest is not None
        if record.session_id != self.sid or record.record_id != f"{self.sid}:{record.sequence}":
            raise ArchiveError("Record identity mismatch")
        if record.sequence <= self._manifest["last_sequence"]:
            found = self._existing(record.sequence)
            if found and found[0] == record:
                return found[1]
            raise ArchiveError("Conflicting or missing history record")
        if record.sequence != self._manifest["last_sequence"] + 1:
            raise ArchiveError("History sequence gap")
        raw = record.model_dump_json().encode("utf-8") + b"\n"
        segments: list[dict[str, Any]] = self._manifest["segments"]
        current = segments[-1] if segments else None
        if current and (current["sealed"] or (
            current["end"] and current["end"] + len(raw) > self.segment_max_bytes
        )):
            self._sync()
            current["sealed"] = True
            current = None
        publish = current is None
        if current is None:
            number = int(segments[-1]["name"][8:-6]) + 1 if segments else 1
            name = f"history-{number:06d}.jsonl"
            with self.path(name).open("xb"):
                pass
            current = {
                "name": name, "first_sequence": 0, "last_sequence": 0,
                "records": 0, "text_bytes": 0, "end": 0,
                "min_time": "", "max_time": "", "sealed": False,
            }
            segments.append(current)
        path = self.path(current["name"])
        with path.open("ab") as stream:
            start = stream.tell()
            stream.write(raw)
            stream.flush()
            end = stream.tell()
            if len(raw) >= self.segment_max_bytes:
                os.fsync(stream.fileno())
                current["sealed"] = True
        self._add_info(current, record)
        current["end"] = end
        current["fingerprint"] = list(self._stat(path) or (0, 0))
        self._manifest["last_sequence"] = record.sequence
        # 普通追加只更新内存目录；滚动和 checkpoint 发布清单，崩溃时从完整尾行恢复。
        if publish or current["sealed"]:
            self._write_manifest()
        return ArchiveReceipt(
            record_id=record.record_id, sequence=record.sequence,
            segment=current["name"], offset=start, end=end,
        )

    # 为新事件分配会话序号并追加，序号分配与写入处于同一锁内
    def emit(
        self, kind: RecordKind, payload: dict[str, Any], run_id: str | None = None,
        *, source: dict[str, Any] | None = None, timestamp: str | None = None,
        prepared: Callable[[HistoryRecord], None] | None = None,
    ) -> ArchiveReceipt:
        with self.transaction():
            assert self._manifest is not None
            sequence = self._manifest["last_sequence"] + 1
            record = HistoryRecord(
                record_id=f"{self.sid}:{sequence}", session_id=self.sid, sequence=sequence,
                timestamp=timestamp or datetime.now(UTC).isoformat(), kind=kind,
                run_id=run_id, payload=payload, source=source or {},
            )
            if prepared is not None:
                prepared(record)
            return self._append(record)

    # 确保所有分片已持久化，调用方持有写锁
    def _sync(self) -> None:
        assert self._manifest is not None
        for segment in self._manifest["segments"]:
            if segment["last_sequence"] > self._manifest["durable_sequence"]:
                with self.path(segment["name"]).open("r+b") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
        self._manifest["durable_sequence"] = self._manifest["last_sequence"]

    # 在压缩、滚动和完成边界执行可观察的耐久性检查
    def checkpoint(self) -> ArchiveCheckpoint:
        with self.transaction():
            self._sync()
            self._write_manifest()
            assert self._manifest is not None
            return ArchiveCheckpoint(
                session_id=self.sid, sequence=self._manifest["durable_sequence"],
            )

    # 流式遍历快照中的完整记录并保留字节定位，损坏会通过 warnings 返回
    def records(
        self, snapshot: dict[str, Any], warnings: list[str],
        check: Callable[[], None] | None = None,
    ) -> Iterator[tuple[HistoryRecord, ArchiveReceipt]]:
        for segment in snapshot["segments"]:
            try:
                stream: BinaryIO
                with self.path(segment["name"]).open("rb") as stream:
                    while stream.tell() < segment["end"]:
                        if check is not None:
                            check()
                        start = stream.tell()
                        raw = stream.readline(segment["end"] - start)
                        if not raw or not raw.endswith(b"\n"):
                            warnings.append(f"incomplete segment: {segment['name']}")
                            break
                        try:
                            record = HistoryRecord.model_validate_json(raw)
                            if record.session_id != self.sid or record.record_id != (
                                f"{self.sid}:{record.sequence}"
                            ):
                                raise ValueError("Wrong session")
                        except ValueError:
                            warnings.append(f"invalid record: {segment['name']}:{start}")
                            continue
                        yield record, ArchiveReceipt(
                            record_id=record.record_id, sequence=record.sequence,
                            segment=segment["name"], offset=start, end=stream.tell(),
                        )
            except OSError as exc:
                warnings.append(f"unreadable segment: {segment['name']}: {exc}")

    # 将已有备份和 thread 导入归档，稳定来源键使中断后重试保持幂等
    def import_legacy(self, check: Callable[[], None] | None = None) -> None:
        with self.transaction(check):
            self._import_legacy(check)

    # 在同一会话文件锁内导入旧数据，与新记录写入互斥
    def _import_legacy(self, check: Callable[[], None] | None) -> None:
        snapshot = self.snapshot()
        if snapshot["legacy_imported"]:
            return
        warnings: list[str] = []
        seen = {
            str(record.source["import_key"])
            for record, _ in self.records(snapshot, warnings, check)
            if "import_key" in record.source
        }
        paths = sorted(self.session_dir.glob("thread_*.jsonl.bak"))
        paths += [self.session_dir / "thread.jsonl", self.session_dir / "notes.md"]
        paths += sorted(self.session_dir.glob("summary_*.md"))
        for path in paths:
            if not path.exists():
                continue
            if path.resolve().parent != self.session_dir:
                raise ArchiveError("Legacy source escapes session")
            occurrences: dict[str, int] = {}
            with path.open("r", encoding="utf-8") as stream:
                lines = enumerate(stream, 1) if ".jsonl" in path.name else [(1, stream.read())]
                for line_number, line in lines:
                    if check is not None:
                        check()
                    kind: RecordKind
                    if ".jsonl" in path.name:
                        try:
                            row = json.loads(line)
                            if not isinstance(row, dict) or row.get("role") not in {
                                "user", "assistant",
                            }:
                                continue
                        except ValueError:
                            continue
                        kind = "user_message" if row["role"] == "user" else "assistant_message"
                    else:
                        kind = "note_snapshot" if path.name == "notes.md" else "compaction_summary"
                        row = {"content": line, "role": "user"}
                    digest = hashlib.sha256(json.dumps(
                        row, ensure_ascii=False, sort_keys=True,
                    ).encode()).hexdigest()
                    occurrences[digest] = occurrences.get(digest, 0) + 1
                    key = f"{kind}:{digest}:{occurrences[digest]}"
                    if key in seen:
                        continue
                    self.emit(
                        kind, {"role": row["role"], "content": row.get("content", "")},
                        row.get("run_id"), timestamp=row.get("ts") or datetime.fromtimestamp(
                            path.stat().st_mtime, UTC,
                        ).isoformat(),
                        source={"file": path.name, "line": line_number, "import_key": key},
                    )
                    seen.add(key)
        with self.transaction():
            assert self._manifest is not None
            self._sync()
            self._manifest["legacy_imported"] = True
            self._write_manifest()
