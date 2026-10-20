from __future__ import annotations

import json
import os
import struct
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.sandbox import (
    SandboxExecRequest,
    SandboxExecResult,
    SandboxPolicyError,
    SandboxType,
    WindowsSandboxConfig,
)
from kama_claude.core.sandbox.errors import (
    SandboxCleanupError,
    SandboxExecutionError,
    SandboxSetupError,
    SandboxUnavailableError,
)
from kama_claude.core.sandbox.windows.hardlinks import audit_hardlinks
from kama_claude.core.sandbox.windows.launcher import _execute_locked, find_mxc
from kama_claude.core.sandbox.windows.process import CapturedProcess
from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry

PROBE = CapturedProcess(SandboxExecResult(0, "", stdout=json.dumps({
    "tier": "base-container", "needsDaclAugmentation": False, "probes": {"bfsCompiledIn": False},
})), "")
DRY_RUN = CapturedProcess(SandboxExecResult(0, "selected isolation tier: base-container"), "")


def request(tmp_path: Path) -> SandboxExecRequest:
    return SandboxExecRequest((str(tmp_path / "wxc-exec.exe"), "--config-base64", "payload"),
                              tmp_path, {}, SandboxType.WINDOWS_MXC)


@pytest.mark.parametrize("result", [
    SandboxExecResult(1, "error"), SandboxExecResult(0, "selected isolation tier: appcontainer-dacl"),
    SandboxExecResult(0, "selected isolation tier: base-container", timed_out=True),
    SandboxExecResult(0, "selected isolation tier: base-container", truncated=True),
])
async def test_dry_run_failure_never_executes_workload(tmp_path, monkeypatch, result) -> None:
    run = AsyncMock(side_effect=[PROBE, CapturedProcess(result, "")])
    monkeypatch.setattr("kama_claude.core.sandbox.windows.process.run_process", run)
    with pytest.raises(SandboxPolicyError):
        await _execute_locked(request(tmp_path))
    assert run.await_count == 2
    assert run.await_args_list[-1].args[0][-1] == "--dry-run"


@pytest.mark.parametrize("code,error", [
    ("backend_unavailable", SandboxUnavailableError), ("unsupported_containment", SandboxUnavailableError),
    ("policy_validation", SandboxPolicyError), ("malformed_request", SandboxPolicyError),
    ("invalid_config", SandboxPolicyError), ("spawn_failed", SandboxSetupError),
    ("setup_failed", SandboxSetupError), ("unknown_backend_failure", SandboxExecutionError),
])
async def test_backend_failure_classification(tmp_path, monkeypatch, code, error) -> None:
    tail = json.dumps({"error": {"code": code, "message": "fixture failure"}}, separators=(",", ":"))
    run = AsyncMock(side_effect=[PROBE, DRY_RUN, CapturedProcess(SandboxExecResult(1, ""), tail)])
    monkeypatch.setattr("kama_claude.core.sandbox.windows.process.run_process", run)
    with pytest.raises(error):
        await _execute_locked(request(tmp_path))
    assert run.await_count == 3


async def test_backend_timeout_is_a_distinct_result(tmp_path, monkeypatch) -> None:
    tail = '{"error":{"code":"timeout","message":"fixture timed out"}}'
    run = AsyncMock(side_effect=[PROBE, DRY_RUN, CapturedProcess(SandboxExecResult(1, "partial"), tail)])
    monkeypatch.setattr("kama_claude.core.sandbox.windows.process.run_process", run)
    result = await _execute_locked(request(tmp_path))
    assert result.timed_out and result.output == "partial"


@pytest.mark.parametrize("error", [SandboxPolicyError, SandboxUnavailableError, SandboxSetupError,
                                   SandboxCleanupError, SandboxExecutionError])
async def test_sandbox_failures_are_not_retried(tmp_path, error) -> None:
    manager = Mock()
    manager.prepare.side_effect = error("fixture failure")
    tool = BashTool(cwd=tmp_path, manager=manager)
    registry = ToolRegistry()
    registry.register(tool)
    result = await invoke_tool(registry, ToolCallBlock(id="t", name="bash", input={"command": "exit 0"}),
                               EventBus(), "run")
    assert result.error_type == error.error_type
    manager.prepare.assert_called_once()


@pytest.mark.parametrize("binary", [b"", b"not a PE", b"MZ" + b"\0" * 62])
def test_invalid_executable_is_rejected(tmp_path, monkeypatch, binary) -> None:
    monkeypatch.setattr("platform.machine", lambda: "AMD64")
    exe = tmp_path / "fake.exe"
    exe.write_bytes(binary)
    with pytest.raises(SandboxUnavailableError, match="Invalid MXC"):
        find_mxc(WindowsSandboxConfig(mxc_executable=exe))


def test_missing_and_wrong_architecture_executable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("platform.machine", lambda: "AMD64")
    exe = tmp_path / "fake.exe"
    config = WindowsSandboxConfig(mxc_executable=exe)
    with pytest.raises(SandboxUnavailableError, match="missing"):
        find_mxc(config)
    header = bytearray(64)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 64)
    exe.write_bytes(header + b"PE\0\0" + struct.pack("<H", 0xAA64))
    with pytest.raises(SandboxUnavailableError, match="architecture"):
        find_mxc(config)
    with pytest.raises(SandboxPolicyError, match="absolute"):
        find_mxc(replace(config, mxc_executable=Path("relative.exe")))


@pytest.mark.parametrize("grant_is_file", [False, True])
def test_hardlink_audit_rejects_alias_without_modifying_files(tmp_path, grant_is_file) -> None:
    work = tmp_path / "work"
    work.mkdir()
    target = tmp_path / "denied"
    target.write_text("original")
    alias = work / "alias"
    os.link(target, alias)
    with pytest.raises(SandboxPolicyError, match="hardlinked"):
        audit_hardlinks((alias if grant_is_file else work,), tmp_path / "runtime" / "mxc.exe")
    assert alias.stat().st_ino == target.stat().st_ino
    assert target.read_text() == "original"


def test_hardlink_audit_allows_ordinary_files_and_fails_closed_on_io_error(tmp_path, monkeypatch) -> None:
    work = tmp_path / "work"
    work.mkdir()
    target = work / "ordinary"
    target.write_text("ok")
    exe = tmp_path / "runtime" / "mxc.exe"
    audit_hardlinks((work,), exe)
    original = os.stat

    def inaccessible(path, *args, **kwargs):
        if path == target:
            raise PermissionError("fixture")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", inaccessible)
    with pytest.raises(SandboxPolicyError, match="Cannot audit"):
        audit_hardlinks((work,), exe)
