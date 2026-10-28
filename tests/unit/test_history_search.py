from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from pathlib import Path

import pytest

from kama_claude.core.history.models import HistoryConfig, HistoryReadParams, HistorySearchParams
from kama_claude.core.history.service import HistoryService, workspace_key
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore


# 创建无模型依赖的会话及真实分片归档
def populate(store: SessionStore, sid: str, workspace: Path | None, texts: list[str]) -> list[str]:
    store.write_meta(Session(
        id=sid, mode="chat", status="closed", title=sid,
        created_at="2026-10-01T00:00:00+00:00", updated_at="2026-10-28T00:00:00+00:00",
        workspace=str(workspace) if workspace else None,
    ))
    archive = store.history(sid)
    return [archive.emit("user_message", {"content": text}, "run",
                         timestamp=f"2026-10-28T00:00:{index:02}+00:00").record_id
            for index, text in enumerate(texts)]


# 功能：扫描和 FTS5 在中文短词、Unicode、路径和标点上的 Top-K 完全一致
# 设计：先取得扫描结果再显式建索引，对比记录身份而非只检查命中数
@pytest.mark.parametrize("query", ["历史", "历史检索", "a", "AUTH_token", "demo.py",
                                  '"quoted"', "a*b", "ＦＯＯ", "auth 历史", "不存在"])
async def test_scan_index_equivalence(tmp_path: Path, query: str) -> None:
    store = SessionStore(tmp_path / "sessions", HistoryConfig(segment_max_bytes=500))
    populate(store, "sess-a", tmp_path, [
        "历史检索 AUTH_token F:/code/demo.py", 'a*b "quoted" FOO',
        "历史 AUTH_token auth_token", "irrelevant",
    ])
    service = HistoryService(store)
    try:
        params = HistorySearchParams(query=query)
        scanned = await service.search(params, tmp_path)
        await service.status(tmp_path, rebuild=True)
        assert service._build_task is not None
        await service._build_task
        assert service.index.error is None
        service.config.backend = "sqlite"
        indexed = await service.search(params, tmp_path)
        assert indexed.backend == "sqlite"
        assert indexed.complete and scanned.complete
        assert indexed.results == scanned.results
    finally:
        await service.close()


# 功能：工作区过滤同样适用于按 ID 读取，未知工作区只能在已授权全局范围中出现
# 设计：用三个不同来源且正文相同的会话，验证检索及读取不会因 ID 已知而绕过范围
async def test_scope_and_read_authorization(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    mine = populate(store, "sess-a", tmp_path, ["shared keyword"])
    others = populate(store, "sess-b", tmp_path / "other", ["shared keyword"])
    populate(store, "sess-old", None, ["shared keyword"])
    service = HistoryService(store)
    try:
        result = await service.search(HistorySearchParams(query="shared"), tmp_path)
        assert [hit.record_id for hit in result.results] == mine
        with pytest.raises(PermissionError):
            await service.read(HistoryReadParams(record_id=others[0]), tmp_path)
        with pytest.raises(PermissionError):
            await service.search(HistorySearchParams(query="shared", scope="all"), tmp_path)
        result = await service.search(
            HistorySearchParams(query="shared", scope="all"), tmp_path,
            frozenset({workspace_key(tmp_path), workspace_key(tmp_path / "other"), None}),
        )
        assert len(result.results) == 3
    finally:
        await service.close()


# 功能：相邻读取跨分片按序号排列，字符分页可完整重组内容
# 设计：每条消息独占分片并使用小分页，避免只覆盖同一文件内读取
async def test_cross_segment_read_and_paging(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions", HistoryConfig(
        segment_max_bytes=200, max_read_chars=100,
    ))
    ids = populate(store, "sess-a", tmp_path, [f"message {index}" for index in range(5)])
    service = HistoryService(store)
    try:
        offset = 0
        parts = []
        while True:
            page = await service.read(HistoryReadParams(record_id=ids[2], offset=offset), tmp_path)
            assert [record.sequence for record in page.records] == [1, 2, 3, 4, 5]
            parts.append(page.text)
            if page.next_offset is None:
                break
            offset = page.next_offset
        assert all(f"message {index}" in "".join(parts) for index in range(5))
    finally:
        await service.close()


# 功能：索引建立后的新增尾部立即可检索，删除分片后不会返回悬空命中
# 设计：不等待新增记录被索引就查询，覆盖索引前缀和实时尾部合并路径
async def test_index_tail_and_removed_segment(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions", HistoryConfig(backend="sqlite"))
    populate(store, "sess-a", tmp_path, ["before needle"])
    service = HistoryService(store)
    try:
        await service.status(tmp_path, rebuild=True)
        assert service._build_task is not None
        await service._build_task
        archive = store.history("sess-a")
        receipt = archive.emit("user_message", {"content": "after needle"})
        found = await service.search(HistorySearchParams(query="needle"), tmp_path)
        assert found.complete and len(found.results) == 2
        if service._build_task is not None:
            await service._build_task
        archive.path(receipt.segment).unlink()
        found = await service.search(HistorySearchParams(query="needle"), tmp_path)
        assert found.results == []
        assert not found.complete
    finally:
        await service.close()


# 功能：超时与数量截断分开表达，空白查询不会被当作匹配所有记录
# 设计：极小预算触发实际扫描取消，再使用正常预算检查 Top-K 完整性标志
async def test_budget_and_truncation(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions", HistoryConfig(search_timeout_seconds=0.000001))
    populate(store, "sess-a", tmp_path, ["needle"] * 3)
    service = HistoryService(store)
    try:
        result = await service.search(HistorySearchParams(query="needle"), tmp_path)
        assert not result.complete and result.reason == "search_timeout"
        service.config.search_timeout_seconds = 3
        result = await service.search(HistorySearchParams(query="needle", limit=1), tmp_path)
        assert result.complete and result.truncated and len(result.results) == 1
        with pytest.raises(ValueError, match="blank"):
            await service.search(HistorySearchParams(query="  "), tmp_path)
    finally:
        await service.close()


# 功能：取消协程会停止真实工作线程，而非留下后台扫描继续占用资源
# 设计：worker 等待取消信号并记录退出，断言取消返回之前线程已经结束
async def test_real_worker_cancellation(tmp_path: Path) -> None:
    service = HistoryService(SessionStore(tmp_path / "sessions"))
    started, finished = threading.Event(), threading.Event()

    # 等待服务发出的真实取消信号
    def work(stop: threading.Event) -> None:
        started.set()
        stop.wait(5)
        finished.set()

    task = asyncio.create_task(service._worker(work))
    await asyncio.to_thread(started.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    await service.close()


# 功能：损坏的索引降级扫描且重建不修改历史文件
# 设计：写坏派生数据库后比较归档字节，避免把清空原始数据误认为修复成功
async def test_corrupt_index_rebuild(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions", HistoryConfig(backend="sqlite"))
    populate(store, "sess-a", tmp_path, ["unique needle"])
    service = HistoryService(store)
    try:
        original = {p.name: p.read_bytes() for p in store.history("sess-a").directory.glob("*.jsonl")}
        service.index.path.write_bytes(b"broken sqlite")
        result = await service.search(HistorySearchParams(query="needle"), tmp_path)
        assert result.backend == "scan" and len(result.results) == 1
        await asyncio.sleep(0)
        assert service._build_task is not None
        await service._build_task
        assert service.index.error is None
        assert original == {p.name: p.read_bytes()
                            for p in store.history("sess-a").directory.glob("*.jsonl")}
    finally:
        await service.close()


# 功能：自动后端按过滤后的候选范围选择，小工作区不被其他大工作区强制索引化
# 设计：调小测试阈值而不创建巨型夹具，检查两个工作区各自的实际后端和索引等价性
async def test_auto_router_uses_filtered_scope(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions", HistoryConfig(index_threshold_bytes=100))
    populate(store, "sess-small", tmp_path, ["needle"])
    populate(store, "sess-large", tmp_path / "large", ["needle " * 100])
    service = HistoryService(store)
    try:
        small = await service.search(HistorySearchParams(query="needle"), tmp_path)
        assert small.backend == "scan" and service._build_task is None
        await service.status(tmp_path / "large", rebuild=True)
        assert service._build_task is not None
        await service._build_task
        large = await service.search(HistorySearchParams(query="needle"), tmp_path / "large")
        assert large.backend == "sqlite" and large.complete
        small = await service.search(HistorySearchParams(query="needle"), tmp_path)
        assert small.backend == "scan" and small.complete
    finally:
        await service.close()


# 功能：时间过滤对带时区和 UTC 时间戳采用相同语义，不错误跳过分片
# 设计：同一时刻使用不同偏移表示，检查精确边界和两个后端的一致命中
async def test_time_filter_normalizes_offsets(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    populate(store, "sess-a", tmp_path, [])
    store.history("sess-a").emit(
        "user_message", {"content": "needle"}, timestamp="2026-10-28T08:00:00+08:00",
    )
    service = HistoryService(store)
    try:
        params = HistorySearchParams(
            query="needle", since=datetime.fromisoformat("2026-10-28T00:00:00+00:00"),
            until=datetime.fromisoformat("2026-10-28T00:00:00+00:00"),
        )
        assert len((await service.search(params, tmp_path)).results) == 1
        await service.status(tmp_path, rebuild=True)
        assert service._build_task is not None
        await service._build_task
        service.config.backend = "sqlite"
        result = await service.search(params, tmp_path)
        assert result.complete and len(result.results) == 1
    finally:
        await service.close()


# 功能：查询与重建并发时结果不读取半成品索引，取消不误杀共享构建
# 设计：同时发出多个真实查询与重建请求，用全部记录身份验证每个完整结果
async def test_concurrent_search_and_rebuild(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions", HistoryConfig(
        backend="sqlite", segment_max_bytes=600,
    ))
    ids = populate(store, "sess-a", tmp_path, [f"needle {index}" for index in range(30)])
    service = HistoryService(store)
    try:
        await service.status(tmp_path, rebuild=True)
        results = await asyncio.gather(*(
            service.search(HistorySearchParams(query="needle", limit=50), tmp_path)
            for _ in range(4)
        ))
        assert all(result.complete for result in results)
        assert all({hit.record_id for hit in result.results} == set(ids) for result in results)
        assert service._build_task is not None
        await service._build_task
        assert service.index.error is None
    finally:
        await service.close()


# 功能：等待归档或索引锁也遵守搜索预算，不必等待持锁线程结束才返回超时
# 设计：由事件循环线程持有锁，让实际检索线程竞争，外层超时用于发现锁等待泄漏
@pytest.mark.parametrize("lock_kind", ["archive", "index"])
async def test_waiting_for_lock_respects_budget(tmp_path: Path, lock_kind: str) -> None:
    store = SessionStore(tmp_path / "sessions", HistoryConfig(search_timeout_seconds=0.1))
    populate(store, "sess-a", tmp_path, ["needle"])
    service = HistoryService(store)
    try:
        await service.status(tmp_path, rebuild=True)
        assert service._build_task is not None
        await service._build_task
        service.config.backend = "sqlite"
        lock = store.history("sess-a")._lock if lock_kind == "archive" else service.index.guard
        with lock:
            result = await asyncio.wait_for(
                service.search(HistorySearchParams(query="needle"), tmp_path), timeout=1,
            )
            assert not result.complete and result.reason == "search_timeout"
    finally:
        await service.close()


# 功能：服务关闭会等待实际查询线程停止，避免关闭后仍有后台读取
# 设计：不显式取消调用任务，只关闭服务，验证协作信号与线程退出顺序
async def test_close_waits_for_query_worker(tmp_path: Path) -> None:
    service = HistoryService(SessionStore(tmp_path / "sessions"))
    started, finished = threading.Event(), threading.Event()

    # 收到服务关闭信号才结束工作线程
    def work(stop: threading.Event) -> None:
        started.set()
        stop.wait(5)
        finished.set()

    task = asyncio.create_task(service._worker(work))
    assert await asyncio.to_thread(started.wait, 1)
    await service.close()
    assert finished.is_set()
    await task


# 功能：重复包含关键词的派生摘要不会挤掉原始证据
# 设计：限制仅返回一条，并让较新摘要包含更多匹配，验证排序并非按重复次数提升摘要
async def test_original_evidence_precedes_derived_summary(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    ids = populate(store, "sess-a", tmp_path, ["needle"])
    store.history("sess-a").emit("compaction_summary", {"content": "needle " * 20})
    service = HistoryService(store)
    try:
        result = await service.search(HistorySearchParams(query="needle", limit=1), tmp_path)
        assert [hit.record_id for hit in result.results] == ids
        assert result.complete and result.truncated
    finally:
        await service.close()
