from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import os
import socket
import sys
from pathlib import Path

import pytest

from kama_claude.core.sandbox import (
    FileSystemPolicy,
    NetworkPolicy,
    SandboxCommand,
    SandboxManager,
    SandboxPolicy,
    spawn,
)
from kama_claude.core.sandbox.models import SandboxExecResult
from kama_claude.core.sandbox.windows.command import powershell_command
from kama_claude.core.sandbox.windows.scratch import process_scratch
from kama_claude.core.tools.builtin.bash import BashTool

pytestmark = [pytest.mark.sandbox_native, pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("KAMA_REQUIRE_WINDOWS_SANDBOX") != "1",
    reason="Set KAMA_REQUIRE_WINDOWS_SANDBOX=1 on a BaseContainer-capable Windows host",
)]


def ps_string(path: Path | str) -> str:
    return "'" + str(path).replace("'", "''") + "'"


async def execute(
    cwd: Path, script: str, *, policy: SandboxPolicy | None = None,
    timeout: float = 10, limit: int = 65536,
) -> SandboxExecResult:
    policy = policy or SandboxPolicy(FileSystemPolicy(read_roots=(), write_roots=(cwd,)))
    with process_scratch(policy, dict(os.environ)) as (prepared, env):
        command = SandboxCommand(powershell_command(script), cwd, env, timeout, limit)
        return await spawn(SandboxManager().prepare(command, prepared))


@pytest.mark.asyncio
async def test_pipes_unicode_exit_and_stdin_eof(tmp_path: Path) -> None:
    result = await execute(tmp_path, "[Console]::WriteLine('中文 stdout'); "
                           "[Console]::Error.WriteLine('stderr'); "
                           "if ([Console]::ReadLine() -ne $null) { exit 9 }; exit 7")
    assert result.returncode == 7
    assert "中文 stdout" in result.stdout
    assert "stderr" in result.stderr


@pytest.mark.asyncio
async def test_filesystem_and_protected_ancestor(tmp_path: Path) -> None:
    work = tmp_path / "workspace with spaces"
    protected = work / "parent" / ".git"
    protected.mkdir(parents=True)
    (protected / "keep").write_text("original")
    secret = tmp_path / "secret"
    secret.write_text("private fixture")
    policy = SandboxPolicy(FileSystemPolicy(
        read_roots=(), write_roots=(work,), protected_paths=(protected,), deny_paths=(secret,),
    ))
    script = f"""
$ErrorActionPreference='Stop'; $r=@{{}};
[IO.File]::WriteAllText({ps_string(work / 'allowed')}, 'ok');
try {{ [IO.File]::WriteAllText({ps_string(protected / 'keep')},'bad'); $r.write=$true }} catch {{ $r.write=$false }}
try {{ [IO.File]::WriteAllText({ps_string(tmp_path / 'outside')},'bad'); $r.outside=$true }} catch {{ $r.outside=$false }}
try {{ $null=[IO.File]::ReadAllText({ps_string(secret)}); $r.read=$true }} catch {{ $r.read=$false }}
try {{ [IO.Directory]::Move({ps_string(protected.parent)},{ps_string(work / 'moved')}); $r.rename=$true }} catch {{ $r.rename=$false }}
$r | ConvertTo-Json -Compress
"""
    result = await execute(work, script, policy=policy)
    assert result.returncode == 0, result.output
    assert json.loads(result.stdout) == {"write": False, "outside": False, "read": False, "rename": False}
    assert (protected / "keep").read_text() == "original"
    assert (work / "allowed").read_text() == "ok"


@pytest.mark.asyncio
async def test_output_truncation_drains_both_streams(tmp_path: Path) -> None:
    result = await execute(tmp_path, "1..10000 | ForEach-Object { "
                           "[Console]::WriteLine('stdout-padding'); "
                           "[Console]::Error.WriteLine('stderr-padding') }", limit=1024)
    assert result.returncode == 0
    assert result.truncated
    assert len(result.output.encode()) <= 1024


@pytest.mark.asyncio
async def test_timeout_and_scratch_cleanup(tmp_path: Path) -> None:
    policy = SandboxPolicy(FileSystemPolicy(read_roots=(), write_roots=(tmp_path,)))
    with process_scratch(policy, dict(os.environ)) as (prepared, env):
        scratch = Path(env["TEMP"])
        command = SandboxCommand(powershell_command("Start-Sleep 20"), tmp_path, env, 1)
        result = await spawn(SandboxManager().prepare(command, prepared))
    assert result.timed_out
    assert scratch.name.startswith("kama-sandbox-") and not scratch.exists()


def process_alive(pid: int) -> bool:
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.restype = ctypes.c_void_p
    api.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    handle = api.OpenProcess(0x100000, False, pid)
    if not handle:
        return False
    try:
        return api.WaitForSingleObject(handle, 0) == 258
    finally:
        api.CloseHandle(handle)


def child_script(marker: Path, *, wait: bool) -> str:
    encoded = base64.b64encode("Start-Sleep 60".encode("utf-16le")).decode()
    return (
        f"$child=Start-Process -FilePath $PSHOME\\powershell.exe -PassThru -WindowStyle Hidden "
        f"-ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','{encoded}'; "
        f"[IO.File]::WriteAllText({ps_string(marker)},[string]$child.Id); "
        + ("Start-Sleep 60" if wait else "exit 0")
    )


@pytest.mark.asyncio
async def test_normal_exit_reaps_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    result = await execute(tmp_path, child_script(marker, wait=False))
    assert result.returncode == 0, result.output
    assert not process_alive(int(marker.read_text()))


@pytest.mark.asyncio
async def test_cancellation_reaps_descendant_and_leaves_other_call(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    cancelled = asyncio.create_task(execute(tmp_path, child_script(marker, wait=True), timeout=90))
    survivor = asyncio.create_task(execute(tmp_path, "Start-Sleep 2; 'survived'"))
    try:
        async with asyncio.timeout(15):
            while not marker.exists():
                if cancelled.done():
                    pytest.fail(str(cancelled.result()))
                await asyncio.sleep(0.05)
        pid = int(marker.read_text())
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert not process_alive(pid)
        assert "survived" in (await survivor).stdout
    finally:
        cancelled.cancel()
        survivor.cancel()
        await asyncio.gather(cancelled, survivor, return_exceptions=True)


@pytest.mark.asyncio
async def test_network_deny_and_outbound_allow(tmp_path: Path) -> None:
    # No application data is sent. Allow an operator-provided reachable external TCP endpoint.
    address, port_text = os.environ.get("KAMA_SANDBOX_TEST_ENDPOINT", "github.com:443").rsplit(":", 1)
    port = int(port_text)
    with socket.create_connection((address, port), timeout=5):
        pass
    script = (
        "$client=[Net.Sockets.TcpClient]::new(); try { "
        f"$task=$client.ConnectAsync('{address}',{port}); "
        "$null=$task.Wait(3000); [Console]::WriteLine($client.Connected) "
        "} catch { [Console]::WriteLine('False') } finally { $client.Dispose() }"
    )
    for mode, expected in ((NetworkPolicy.DENY, "False"), (NetworkPolicy.ALLOW, "True")):
        policy = SandboxPolicy(FileSystemPolicy(read_roots=(), write_roots=(tmp_path,)), mode)
        result = await execute(tmp_path, script, policy=policy)
        assert result.returncode == 0 and result.stdout.strip() == expected, result.output


@pytest.mark.asyncio
async def test_host_loopback_stays_denied(tmp_path: Path) -> None:
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        script = (
            "$client=[Net.Sockets.TcpClient]::new(); try { "
            f"$task=$client.ConnectAsync('127.0.0.1',{port}); "
            "$null=$task.Wait(1500); [Console]::WriteLine($client.Connected) "
            "} catch { [Console]::WriteLine('False') } finally { $client.Dispose() }"
        )
        for mode in NetworkPolicy:
            policy = SandboxPolicy(FileSystemPolicy(read_roots=(), write_roots=(tmp_path,)), mode)
            result = await execute(tmp_path, script, policy=policy)
            assert result.returncode == 0 and result.stdout.strip() == "False", result.output
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_bash_tool_uses_powershell(tmp_path: Path) -> None:
    result = await BashTool(cwd=tmp_path).invoke({"command": "'hello'; exit 0"})
    assert not result.is_error, result.content
    assert "hello" in result.content
