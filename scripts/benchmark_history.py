from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from kama_claude.core.history.models import HistoryConfig, HistorySearchParams
from kama_claude.core.history.service import HistoryService
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore


# 返回进程峰值工作集，包含 Python 和 SQLite 的本地内存分配
def peak_bytes() -> int:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "peak", "working", "peak_paged", "paged", "peak_nonpaged", "nonpaged",
                    "pagefile", "peak_pagefile",
                )
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.c_void_p(-1), ctypes.byref(counters), counters.cb,
        )
        return int(counters.peak)
    import resource
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * (
        1 if sys.platform == "darwin" else 1024
    )


# 生成有真实分片边界的临时归档并测量直接查询、索引构建和短词退化
async def benchmark(root: Path, mib: int) -> dict[str, Any]:
    config = HistoryConfig(backend="scan")
    store = SessionStore(root / f"{mib}mib", config)
    workspace = root.resolve()
    store.write_meta(Session(
        id="sess-bench", title="Synthetic benchmark", mode="chat", status="closed",
        created_at="2026-10-28T00:00:00+00:00", updated_at="2026-10-28T00:00:00+00:00",
        workspace=str(workspace),
    ))
    archive = store.history("sess-bench")
    block = ("ordinary history retrieval text 历史记录 " * 2000)[:32_768]
    target = mib * 1024 * 1024
    total, records = 0, 0
    start = time.perf_counter()
    while total < target:
        records += 1
        text = f"unique_record_{records:08d} " + block
        archive.emit("user_message", {"content": text}, "benchmark")
        total += len(text.encode("utf-8"))
    archive.checkpoint()
    generated = time.perf_counter() - start
    print(f"{mib} MiB generated: {records} records in {generated:.2f}s", flush=True)
    service = HistoryService(store)
    try:
        query = HistorySearchParams(query=f"unique_record_{records:08d}")
        start = time.perf_counter()
        scanned = await service.search(query, workspace)
        scan_seconds = time.perf_counter() - start
        start = time.perf_counter()
        await service.status(workspace, rebuild=True)
        assert service._build_task is not None
        await service._build_task
        build_seconds = time.perf_counter() - start
        if service.index.error:
            raise RuntimeError(service.index.error)
        print(f"{mib} MiB index built in {build_seconds:.2f}s", flush=True)
        config.backend = "sqlite"
        start = time.perf_counter()
        indexed = await service.search(query, workspace)
        indexed_seconds = time.perf_counter() - start
        start = time.perf_counter()
        short = await service.search(HistorySearchParams(query="历史"), workspace)
        short_seconds = time.perf_counter() - start
        status = await service.status(workspace)
        result = {
            "target_mib": mib, "records": records, "segments": status["segments"],
            "generated_seconds": round(generated, 3),
            "scan_seconds": round(scan_seconds, 3), "scan_complete": scanned.complete,
            "build_seconds": round(build_seconds, 3),
            "indexed_seconds": round(indexed_seconds, 3), "indexed_complete": indexed.complete,
            "indexed_hits": len(indexed.results), "short_seconds": round(short_seconds, 3),
            "short_complete": short.complete, "process_peak_bytes": peak_bytes(),
        }
        assert indexed.complete and len(indexed.results) == 1
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result
    finally:
        await service.close()


# 在用户指定的工作目录中创建独立样本目录并保存可复查的测量结果
async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", nargs="+", type=int, default=[10, 100, 1024])
    parser.add_argument("--root", type=Path, default=Path("workspace"))
    args = parser.parse_args()
    if any(size <= 0 for size in args.sizes):
        parser.error("sizes must be positive MiB values")
    root = args.root.resolve() / f"history-benchmark-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    results = [await benchmark(root, size) for size in args.sizes]
    output = root / "results.json"
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Results: {output}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
