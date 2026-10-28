from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.context import AgentContext, ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.history.archive import ArchiveError, HistoryArchive
from kama_claude.core.history.models import HistoryRecord
from kama_claude.core.llm.types import LlmResponse
from kama_claude.core.session.store import SessionStore


# 创建具有稳定身份的测试消息
def record(sequence: int = 1, content: str = "history") -> HistoryRecord:
    return HistoryRecord(
        record_id=f"sess-test:{sequence}", session_id="sess-test", sequence=sequence,
        timestamp="2026-10-28T00:00:00+00:00", kind="user_message", payload={"content": content},
    )


# 功能：滚动按真实 UTF-8 字节执行，封存文件保持原样且逻辑序号连续
# 设计：阈值只允许一条中文消息，比较首片字节内容并跨所有分片重读原文
def test_rollover_and_stable_identity(tmp_path: Path) -> None:
    archive = HistoryArchive(tmp_path / "sess-test", segment_max_bytes=400)
    first = archive.append_record(record(content="历史检索" * 10))
    original = archive.path(first.segment).read_bytes()
    for sequence in range(2, 6):
        archive.append_record(record(sequence, "历史检索" * 10))
    checkpoint = archive.checkpoint()
    snapshot = archive.snapshot()
    assert len(snapshot["segments"]) == 5
    assert archive.path(first.segment).read_bytes() == original
    assert checkpoint.sequence == 5
    rows = list(archive.records(snapshot, []))
    assert [item.sequence for item, _ in rows] == [1, 2, 3, 4, 5]
    assert all(item.payload["content"] == "历史检索" * 10 for item, _ in rows)


# 功能：超大单条记录独占分片，下一条消息进入新片且正文不被截断
# 设计：阈值小于一条消息，以真实序列化长度检查超限及封存行为
def test_oversized_record(tmp_path: Path) -> None:
    archive = HistoryArchive(tmp_path / "sess-test", segment_max_bytes=300)
    receipt = archive.append_record(record(content="x" * 1000))
    second = archive.append_record(record(2))
    assert receipt.segment != second.segment
    snapshot = archive.snapshot()
    assert snapshot["segments"][0]["sealed"]
    assert snapshot["segments"][0]["end"] > 300
    assert list(archive.records(snapshot, []))[0][0].payload["content"] == "x" * 1000


# 功能：完整记录重试幂等，身份相同但内容不同的提交被拒绝
# 设计：重开归档实例后再次提交，覆盖进程重启而非仅内存缓存去重
def test_idempotent_append_after_restart(tmp_path: Path) -> None:
    archive = HistoryArchive(tmp_path / "sess-test")
    receipt = archive.append_record(record())
    reopened = HistoryArchive(tmp_path / "sess-test")
    assert reopened.append_record(record()) == receipt
    with pytest.raises(ArchiveError, match="Conflicting"):
        reopened.append_record(record(content="changed"))
    assert len(list(reopened.records(reopened.snapshot(), []))) == 1


# 功能：清单丢失与未完成尾行不会破坏已完整写入的历史
# 设计：模拟写入中断后删除缓存清单，确认恢复文件保留尾部且新序号继续递增
def test_manifest_and_partial_tail_recovery(tmp_path: Path) -> None:
    archive = HistoryArchive(tmp_path / "sess-test")
    receipt = archive.append_record(record())
    with archive.path(receipt.segment).open("ab") as stream:
        stream.write(b'{"interrupted":')
    archive.path("manifest.json").unlink()
    recovered = HistoryArchive(tmp_path / "sess-test")
    snapshot = recovered.snapshot()
    assert snapshot["last_sequence"] == 1
    assert list(recovered.directory.glob("recovery-*.bin"))[0].read_bytes() == b'{"interrupted":'
    recovered.append_record(record(2))
    assert len(list(recovered.records(recovered.snapshot(), []))) == 2


# 功能：删除分片时不复用原序号，并明确报告历史缺口
# 设计：保留清单但移除封存片，验证目录重建不把缺失记录当成完整结果
def test_missing_segment_preserves_sequence(tmp_path: Path) -> None:
    archive = HistoryArchive(tmp_path / "sess-test", 200)
    first = archive.append_record(record())
    archive.append_record(record(2))
    archive.path(first.segment).unlink()
    snapshot = archive.snapshot()
    assert any("missing segment" in warning for warning in snapshot["warnings"])
    assert archive.emit("user_message", {"content": "new"}).sequence == 3


# 功能：旧备份重复导入保持幂等，真实重复提问仍保留两次
# 设计：同一文件包含两条完全相同记录并复制为备份，区分副本去重与消息去重
def test_legacy_import_keeps_repeated_messages(tmp_path: Path) -> None:
    directory = tmp_path / "sess-test"
    directory.mkdir()
    row = json.dumps({"role": "user", "content": "repeat", "ts": "2026-01-01"}) + "\n"
    (directory / "thread.jsonl").write_text(row * 2, encoding="utf-8")
    (directory / "thread_001.jsonl.bak").write_text(row * 2, encoding="utf-8")
    archive = HistoryArchive(directory)
    archive.import_legacy()
    archive.import_legacy()
    rows = list(archive.records(archive.snapshot(), []))
    assert len(rows) == 2
    assert all(item.payload["content"] == "repeat" for item, _ in rows)


# 功能：清单重建不会把已经归档的新消息从 thread 再次导入
# 设计：走真实归档和 thread 双写路径后移除清单，再用全新存储实例模拟进程重启
def test_manifest_rebuild_does_not_reimport_current_thread(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    archive = store.history("sess-test")
    agent = AgentContext(session_id="sess-test", archive=archive, history_run_id="run")
    agent.add_user_message("native archived message")
    store.save_agent(agent, "run")
    archive.path("manifest.json").unlink()
    reopened = SessionStore(store.root).history("sess-test")
    rows = list(reopened.records(reopened.snapshot(), []))
    assert len(rows) == 1 and rows[0][0].record_id == "sess-test:1"


# 功能：压缩必须等待归档 checkpoint 成功，失败时保留内存原文
# 设计：只让耐久性边界失败，模型仍成功生成摘要，避免误测模型错误分支
async def test_compact_durability_failure_keeps_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = HistoryArchive(tmp_path / "sess-test")
    agent = AgentContext(session_id="sess-test", archive=archive)
    agent.add_user_message("unique original request")
    context = ExecutionContext("run", "goal", agent_context=agent)
    provider = AsyncMock()
    provider.chat.return_value = LlmResponse(stop_reason="end_turn", text="summary")
    monkeypatch.setattr(archive, "checkpoint", lambda: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError, match="disk"):
        await Compactor(EventBus(), tmp_path, "sess-test").compact(context, provider)
    assert context.messages == [{"role": "user", "content": "unique original request"}]


# 功能：多次自动压缩后原始工具输出仍完整留存且摘要具有覆盖边界
# 设计：通过真实 Context 追加入口写消息，压缩两次后直接验证归档而非压缩后的 thread
async def test_compact_keeps_original_tool_evidence(tmp_path: Path) -> None:
    archive = HistoryArchive(tmp_path / "sess-test", 400)
    agent = AgentContext(session_id="sess-test", archive=archive, history_run_id="run-1")
    agent.add_user_message("original")
    context = ExecutionContext("run-1", "goal", agent_context=agent)
    context.add_assistant_message([
        {"type": "tool_use", "id": "t", "name": "read_file", "input": {"path": "a.py"}},
    ])
    context.add_tool_result("t", "tool evidence " * 1000)
    provider = AsyncMock()
    provider.chat.return_value = LlmResponse(stop_reason="end_turn", text="summary")
    compactor = Compactor(EventBus(), tmp_path, "sess-test")
    await compactor.compact(context, provider)
    await compactor.compact(context, provider)
    rows = [item for item, _ in archive.records(archive.snapshot(), [])]
    assert len(rows) == 5
    assert rows[2].payload["content"][0]["content"] == "tool evidence " * 1000
    assert rows[-1].source["covers_through_sequence"] == 4


# 功能：归档写入失败时工具原文留在内存，恢复后同一记录身份只提交一次
# 设计：模拟原文已经追加但清单替换失败，再重试待提交队列以覆盖最危险的半成功路径
def test_tool_result_retry_keeps_memory_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = HistoryArchive(tmp_path / "sess-test", 300)
    agent = AgentContext(session_id="sess-test", archive=archive)
    context = ExecutionContext("run", "goal", agent_context=agent)
    context.add_assistant_message([
        {"type": "tool_use", "id": "t", "name": "bash", "input": {"command": "example"}},
    ])
    original = archive._write_manifest

    # 模拟写入数据后清单暂时不可发布
    def fail_manifest() -> None:
        raise OSError("manifest unavailable")

    monkeypatch.setattr(archive, "_write_manifest", fail_manifest)
    with pytest.raises(OSError, match="manifest"):
        context.add_tool_result("t", "completed tool output")
    assert context.messages[-1]["content"][0]["content"] == "completed tool output"
    assert len(agent.pending_history) == 1
    pending_id = agent.pending_history[0].record.record_id
    monkeypatch.setattr(archive, "_write_manifest", original)
    agent.flush_history()
    assert agent.pending_history == []
    records = [record for record, _ in archive.records(archive.snapshot(), [])]
    assert sum(record.record_id == pending_id for record in records) == 1
