from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kama_claude.core.context import AgentContext
from kama_claude.core.history.archive import HistoryArchive
from kama_claude.core.history.models import HistoryConfig
from kama_claude.core.session.model import Session

logger = logging.getLogger(__name__)

MessageContent = str | list[dict[str, Any]]


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionStore:
    # 初始化 session 文件存储根目录
    def __init__(self, root: Path, history_config: HistoryConfig | None = None) -> None:
        self._root = root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self.history_config = history_config or HistoryConfig()
        self._archives: dict[str, HistoryArchive] = {}
        self._archive_lock = threading.RLock()
        self._history_imported: set[str] = set()

    @property
    # 返回受校验的会话存储根目录供历史服务复用
    def root(self) -> Path:
        return self._root

    # 返回会话唯一归档实例，导入已有记录后才允许写入新历史
    def history(self, sid: str, check: Callable[[], None] | None = None) -> HistoryArchive:
        with self._archive_lock:
            if sid not in self._archives:
                self._archives[sid] = HistoryArchive(
                    self.session_dir(sid), self.history_config.segment_max_bytes,
                )
            archive = self._archives[sid]
            if sid in self._history_imported:
                return archive
        archive.import_legacy(check)
        with self._archive_lock:
            self._history_imported.add(sid)
        return archive

    # 返回指定 session 的目录路径
    def session_dir(self, sid: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", sid):
            raise ValueError("Invalid session ID")
        path = (self._root / sid).resolve()
        if path.parent != self._root:
            raise ValueError("Session directory is outside the session store")
        return path

    # 解析会话文件并拒绝指向会话目录之外的链接
    def _file(self, sid: str, name: str) -> Path:
        directory = self.session_dir(sid)
        path = (directory / name).resolve()
        if path.parent != directory:
            raise ValueError("Session file is outside the session directory")
        return path

    # 返回指定 session 下的 runs 目录路径
    def runs_dir(self, sid: str) -> Path:
        return self.session_dir(sid) / "runs"

    # 将 session meta 写入 meta.json
    def write_meta(self, session: Session) -> None:
        path = self.session_dir(session.id)
        path.mkdir(parents=True, exist_ok=True)
        target = self._file(session.id, "meta.json")
        temporary = path / f".meta-{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(
                json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    # 从 meta.json 读取 session meta
    def read_meta(self, sid: str) -> Session:
        data = json.loads(self._file(sid, "meta.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid session metadata")
        session = Session.from_dict(data)
        if session.id != sid:
            raise ValueError("Session ID does not match its directory")
        return session

    # 扫描有效会话元数据并按最近更新时间排序，损坏记录只记录日志
    def list_meta(self, check: Callable[[], None] | None = None) -> list[Session]:
        sessions = []
        for path in self._root.iterdir():
            if check is not None:
                check()
            if not path.is_dir():
                continue
            try:
                sessions.append(self.read_meta(path.name))
            except (OSError, ValueError, KeyError, TypeError):
                logger.warning("skip invalid session metadata sid=%s", path.name)
        return sorted(sessions, key=lambda s: (s.updated_at, s.id), reverse=True)

    # 追加一条 Anthropic API 消息到 thread.jsonl
    def append_message(
        self,
        sid: str,
        role: str,
        content: MessageContent,
        run_id: str | None = None,
    ) -> None:
        row: dict[str, Any] = {"ts": _now(), "role": role, "content": content}
        if run_id is not None:
            row["run_id"] = run_id
        path = self.session_dir(sid)
        path.mkdir(parents=True, exist_ok=True)
        with self._file(sid, "thread.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 批量追加一次 run 新产生的消息到 thread.jsonl
    def append_messages(
        self,
        sid: str,
        messages: list[dict[str, Any]],
        run_id: str,
    ) -> None:
        for msg in messages:
            self.append_message(
                sid,
                role=str(msg["role"]),
                content=msg["content"],
                run_id=run_id,
            )

    # 提交主 agent 的新增历史，失败后改用完整替换避免部分追加重复
    def save_agent(self, agent: AgentContext, run_id: str) -> None:
        agent.flush_history()
        try:
            if agent.history_replaced:
                self.write_compacted(agent.session_id, agent.messages)
            else:
                self.append_messages(
                    agent.session_id, agent.messages[agent.persisted_count:], run_id,
                )
        except Exception:
            agent.history_replaced = True
            raise
        agent.persisted_count = len(agent.messages)
        agent.history_replaced = False
        if agent.archive is not None:
            agent.archive.checkpoint()

    # 读取完整 thread 并返回可直接传给 Anthropic 的 messages
    def read_messages(self, sid: str) -> list[dict[str, Any]]:
        path = self._file(sid, "thread.jsonl")
        if not path.exists():
            return []

        messages: list[dict[str, Any]] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skip broken thread row sid=%s line=%s", sid, line_no)
                continue
            if not isinstance(row, dict):
                logger.warning("skip invalid thread row sid=%s line=%s", sid, line_no)
                continue
            content = row.get("content", "")
            if not isinstance(content, (str, list)) or (
                isinstance(content, list) and not all(isinstance(b, dict) for b in content)
            ):
                logger.warning("skip invalid message content sid=%s line=%s", sid, line_no)
                continue
            role = row.get("role")
            if role not in ("user", "assistant"):
                logger.warning(
                    "skip unknown thread role sid=%s line=%s role=%s",
                    sid,
                    line_no,
                    role,
                )
                continue
            messages.append({"role": role, "content": row.get("content", "")})

        messages = self._trim_orphan_tool_use(messages)
        from kama_claude.core.compact.budget import truncate_tool_results
        return truncate_tool_results(messages)

    # 裁掉尾部未配对 tool_use 以及其后的消息，避免 Anthropic messages.invalid
    def _trim_orphan_tool_use(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pending: set[str] = set()
        last_balanced = 0
        for idx, msg in enumerate(messages, start=1):
            content = msg.get("content")
            if isinstance(content, list):
                if msg.get("role") == "assistant":
                    for block in content:
                        if block.get("type") == "tool_use":
                            pending.add(str(block.get("id", "")))
                elif msg.get("role") == "user":
                    for block in content:
                        if block.get("type") == "tool_result":
                            pending.discard(str(block.get("tool_use_id", "")))
            if not pending:
                last_balanced = idx
        if pending:
            logger.warning("trim orphan tool_use blocks from thread")
            return messages[:last_balanced]
        return messages

    # 将压缩后的消息对覆盖写入 thread.jsonl，原文件备份为 thread_<ts>.jsonl.bak
    def write_compacted(self, sid: str, messages: list[dict[str, Any]]) -> None:
        path = self._file(sid, "thread.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        ts_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        bak = self._file(sid, f"thread_{ts_str}.jsonl.bak")
        temporary = path.parent / f".thread-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8") as f:
                for msg in messages:
                    row = {"ts": _now(), "role": msg["role"], "content": msg["content"]}
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if path.exists():
                shutil.copyfile(path, bak)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    # 读取 notes.md 全文，文件不存在时返回空字符串
    def read_notes(self, sid: str) -> str:
        path = self._file(sid, "notes.md")
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    # 将一条主动笔记追加到 notes.md
    def append_note(self, sid: str, content: str, run_id: str) -> None:
        self.history(sid).emit("note_snapshot", {"content": content}, run_id)
        path = self.session_dir(sid)
        path.mkdir(parents=True, exist_ok=True)
        with self._file(sid, "notes.md").open("a", encoding="utf-8") as f:
            f.write(f"## Note ({_now()}, {run_id})\n{content}\n\n")
