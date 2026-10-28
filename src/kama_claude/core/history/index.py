from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from kama_claude.core.history.archive import HistoryArchive
from kama_claude.core.history.models import HistoryRecord, normalize, record_text


class SearchStopped(Exception):
    pass


# 同时检查实际工作线程的取消标志与单次查询预算
def check_stop(stop: threading.Event, deadline: float) -> None:
    if stop.is_set() or time.perf_counter() >= deadline:
        raise SearchStopped()


# 流式计算完整前缀指纹，支持追加检测且不将分片全部装入内存
def prefix_hash(path: Path, end: int, stop: threading.Event, deadline: float) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        remaining = end
        while remaining:
            check_stop(stop, deadline)
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise OSError("Segment shortened while indexing")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


class HistoryIndex:
    # 索引仅缓存投影和定位，原始内容始终从会话归档验证读取
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.path = self.root / "history-index.sqlite3"
        self.building = self.root / "history-index.building.sqlite3"
        self.guard = threading.RLock()
        self.error: str | None = None
        self.processed_segments = 0
        self.total_segments = 0

    # 等待索引发布或其他读取时同样遵守实际取消和查询预算
    @contextmanager
    def _locked(
        self, stop: threading.Event | None = None, deadline: float = float("inf"),
    ) -> Iterator[None]:
        while not self.guard.acquire(timeout=0.05):
            if stop is not None:
                check_stop(stop, deadline)
        try:
            if stop is not None:
                check_stop(stop, deadline)
            yield
        finally:
            self.guard.release()

    # 拒绝缓存文件通过符号链接跳出会话存储根目录
    def _safe(self, path: Path) -> Path:
        if path.resolve().parent != self.root:
            raise OSError("History index escapes session root")
        return path

    # 建立可重建的普通表与外部内容 FTS5 表
    def _open(self, path: Path, timeout: float = 0.2) -> sqlite3.Connection:
        connection = sqlite3.connect(self._safe(path), timeout=timeout)
        connection.row_factory = sqlite3.Row
        return connection

    # 初始化缓存版本和触发器，历史归档格式不随索引格式变化
    def _initialize(self, connection: sqlite3.Connection) -> None:
        connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY, rid TEXT UNIQUE NOT NULL, sid TEXT NOT NULL,
                sequence INTEGER NOT NULL, segment TEXT NOT NULL, offset INTEGER NOT NULL,
                end INTEGER NOT NULL, normalized TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS records_source ON records(sid, segment);
            CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
                normalized, content='records', content_rowid='id', tokenize='trigram',
                detail='none', columnsize=0
            );
            CREATE TRIGGER IF NOT EXISTS record_insert AFTER INSERT ON records BEGIN
                INSERT INTO search(rowid, normalized) VALUES(new.id, new.normalized);
            END;
            CREATE TRIGGER IF NOT EXISTS record_delete AFTER DELETE ON records BEGIN
                INSERT INTO search(search, rowid, normalized)
                    VALUES('delete', old.id, old.normalized);
            END;
            CREATE TABLE IF NOT EXISTS segments (
                sid TEXT NOT NULL, name TEXT NOT NULL, end INTEGER NOT NULL,
                fingerprint TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY(sid, name)
            );
            PRAGMA user_version=1;
        """)

    # 查询索引是否存在且版本可用，损坏时允许服务降级扫描
    def available(
        self, stop: threading.Event | None = None, deadline: float = float("inf"),
    ) -> bool:
        if not self.path.exists():
            return False
        try:
            with self._locked(stop, deadline), closing(self._open(self.path)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                connection.execute("SELECT count(*) FROM segments").fetchone()
                if version != 1:
                    self.error = "History index schema version changed"
                    return False
                return True
        except (OSError, sqlite3.Error) as exc:
            self.error = str(exc)
            return False

    # 计算当前快照中可安全复用的索引前缀，变化的封存片回退原文
    def coverage(
        self, archive: HistoryArchive, snapshot: dict[str, Any],
        stop: threading.Event, deadline: float,
    ) -> dict[str, int]:
        coverage: dict[str, int] = {}
        with self._locked(stop, deadline), closing(self._open(self.path)) as connection:
            rows = {row["name"]: row for row in connection.execute(
                "SELECT * FROM segments WHERE sid=?", (archive.sid,),
            )}
        for segment in snapshot["segments"]:
            check_stop(stop, deadline)
            row = rows.get(segment["name"])
            if row is None or row["end"] > segment["end"]:
                continue
            unchanged = row["fingerprint"] == json.dumps(segment["fingerprint"])
            if unchanged or row["digest"] == prefix_hash(
                archive.path(segment["name"]), row["end"], stop, deadline,
            ):
                coverage[segment["name"]] = row["end"]
        return coverage

    # 返回索引匹配的位置，短查询以受预算约束的缓存扫描保证语义
    def candidates(
        self, sid: str, terms: list[str], coverage: dict[str, int],
        stop: threading.Event, deadline: float,
    ) -> Iterator[sqlite3.Row]:
        long_terms = [term for term in terms if len(term) >= 3]
        with self._locked(stop, deadline), closing(self._open(self.path)) as connection:
            connection.set_progress_handler(
                lambda: int(stop.is_set() or time.perf_counter() >= deadline), 1000,
            )
            if long_terms and not any("\0" in term for term in terms):
                # 无位置索引只保存三元组候选，完整子串仍由共享匹配器确认。
                grams = sorted({term[index:index + 3]
                                for term in long_terms for index in range(len(term) - 2)})
                expression = " AND ".join('"' + gram.replace('"', '""') + '"'
                                          for gram in grams[:128])
                cursor = connection.execute(
                    "SELECT r.* FROM records r JOIN search ON search.rowid=r.id "
                    "WHERE search MATCH ? AND r.sid=?", (expression, sid),
                )
            else:
                cursor = connection.execute("SELECT * FROM records WHERE sid=?", (sid,))
            for row in cursor:
                check_stop(stop, deadline)
                if row["end"] <= coverage.get(row["segment"], 0) and all(
                    term in row["normalized"] for term in terms
                ):
                    yield row

    # 构建或增量维护缓存，逐片提交以便中断后继续
    def build(
        self, sources: list[tuple[HistoryArchive, dict[str, Any]]],
        stop: threading.Event, *, rebuild: bool = False,
    ) -> None:
        publish = rebuild or not self.available(stop)
        path = self.building if publish else self.path
        connection = self._open(path, timeout=5)
        try:
            try:
                self._initialize(connection)
            except sqlite3.DatabaseError:
                if not publish:
                    raise
                connection.close()
                for suffix in ("", "-wal", "-shm"):
                    self._safe(Path(str(self.building) + suffix)).unlink(missing_ok=True)
                connection = self._open(self.building)
                self._initialize(connection)
            connection.set_progress_handler(lambda: int(stop.is_set()), 1000)
            if rebuild:
                connection.execute("DELETE FROM records")
                connection.execute("DELETE FROM segments")
                connection.commit()
            self.processed_segments = 0
            self.total_segments = sum(len(snapshot["segments"]) for _, snapshot in sources)
            for archive, snapshot in sources:
                names = {segment["name"] for segment in snapshot["segments"]}
                for row in connection.execute(
                    "SELECT name FROM segments WHERE sid=?", (archive.sid,),
                ).fetchall():
                    if row[0] not in names:
                        connection.execute(
                            "DELETE FROM records WHERE sid=? AND segment=?", (archive.sid, row[0]),
                        )
                        connection.execute(
                            "DELETE FROM segments WHERE sid=? AND name=?", (archive.sid, row[0]),
                        )
                connection.commit()
                for segment in snapshot["segments"]:
                    check_stop(stop, float("inf"))
                    self._update_segment(connection, archive, segment, stop)
                    self.processed_segments += 1
            result = connection.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise sqlite3.DatabaseError(str(result))
            if publish:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("PRAGMA journal_mode=DELETE")
        finally:
            connection.close()
        if publish:
            with self._locked(stop):
                if self.path.exists():
                    try:
                        with closing(self._open(self.path, timeout=5)) as old:
                            old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                            old.execute("PRAGMA journal_mode=DELETE")
                    except sqlite3.DatabaseError:
                        pass
                    for suffix in ("-wal", "-shm"):
                        self._safe(Path(str(self.path) + suffix)).unlink(missing_ok=True)
                self._safe(self.building).replace(self._safe(self.path))
        self.error = None

    # 校验旧前缀后仅处理新增完整行，变更或截断时重建单个分片
    def _update_segment(
        self, connection: sqlite3.Connection, archive: HistoryArchive,
        segment: dict[str, Any], stop: threading.Event,
    ) -> None:
        name = segment["name"]
        path = archive.path(name)
        row = connection.execute(
            "SELECT * FROM segments WHERE sid=? AND name=?", (archive.sid, name),
        ).fetchone()
        if row and row["fingerprint"] == json.dumps(segment["fingerprint"]):
            return
        offset = 0
        if row and row["end"] <= segment["end"] and row["digest"] == prefix_hash(
            path, row["end"], stop, float("inf"),
        ):
            offset = row["end"]
        with connection:
            if offset == 0:
                connection.execute(
                    "DELETE FROM records WHERE sid=? AND segment=?", (archive.sid, name),
                )
            with path.open("rb") as stream:
                stream.seek(offset)
                while stream.tell() < segment["end"]:
                    check_stop(stop, float("inf"))
                    start = stream.tell()
                    raw = stream.readline(segment["end"] - start)
                    if not raw or not raw.endswith(b"\n"):
                        raise OSError("Incomplete history segment")
                    record = HistoryRecord.model_validate_json(raw)
                    if (record.session_id != archive.sid
                            or record.record_id != f"{archive.sid}:{record.sequence}"):
                        raise ValueError("History record identity mismatch")
                    text = normalize(record_text(record))
                    connection.execute(
                        "INSERT OR IGNORE INTO records "
                        "(rid,sid,sequence,segment,offset,end,normalized) VALUES (?,?,?,?,?,?,?)",
                        (record.record_id, archive.sid, record.sequence, name, start,
                         stream.tell(), text),
                    )
            digest = prefix_hash(path, segment["end"], stop, float("inf"))
            connection.execute(
                "INSERT OR REPLACE INTO segments VALUES (?,?,?,?,?)",
                (archive.sid, name, segment["end"], json.dumps(segment["fingerprint"]), digest),
            )
