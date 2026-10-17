from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator

import pytest


# 功能：仅在具备真实平台后端时运行操作系统沙盒集成测试
# 设计：普通跨平台测试显式跳过缺失依赖；设置必测变量后缺失依赖直接失败
@pytest.fixture
def native_sandbox() -> None:
    from pathlib import Path

    from kama_claude.core.sandbox import SandboxUnavailableError

    try:
        if sys.platform == "linux":
            from kama_claude.core.sandbox.linux.launcher import find_bwrap
            from kama_claude.core.sandbox.linux.seccomp import load_seccomp

            find_bwrap()
            load_seccomp()
        elif sys.platform == "darwin":
            if not Path("/usr/bin/sandbox-exec").exists():
                raise SandboxUnavailableError("sandbox-exec is unavailable")
        else:
            raise SandboxUnavailableError("Native sandbox tests require Linux or macOS")
    except SandboxUnavailableError as exc:
        if os.environ.get("KAMA_REQUIRE_NATIVE_SANDBOX") == "1":
            pytest.fail(str(exc))
        pytest.skip(str(exc))


@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return port  # socket released; daemon can bind to this port


@pytest.fixture
async def running_daemon(free_port: int) -> AsyncGenerator[subprocess.Popen[bytes], None]:
    env = os.environ.copy()
    env["KAMA_PORT"] = str(free_port)
    env["KAMA_LOG_FILE"] = ""
    env["KAMA_LOG_LEVEL"] = "WARNING"

    proc = subprocess.Popen([sys.executable, "-m", "kama_claude.core"], env=env)

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
            writer.close()
            await writer.wait_closed()
            break
        except (ConnectionRefusedError, OSError):
            pass
    else:
        proc.terminate()
        proc.wait()
        pytest.fail("Daemon did not start within 3 seconds")

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
