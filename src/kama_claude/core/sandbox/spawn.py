from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys

from .denial import is_setup_failure, is_suspected_denial
from .errors import SandboxSetupError, UnsupportedSandboxError
from .models import SandboxExecRequest, SandboxExecResult, SandboxType


# 清理同一进程组中的命令及子进程，防止超时后后台任务继续执行
async def _stop(proc: asyncio.subprocess.Process) -> None:
    if sys.platform == "win32":
        raise UnsupportedSandboxError("Process-group cleanup requires Linux or macOS")
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    # asyncio 的 wait 可能等待被脱离进程组的后代继承的管道，回收必须有界
    try:
        async with asyncio.timeout(2):
            await proc.wait()
    except TimeoutError:
        # 关闭本地管道以解除传输层等待；不把取消转成普通重试
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()


# 持续排空输出管道但只保留限额内的字节，防止大输出耗尽内存
async def _read_output(
    stream: asyncio.StreamReader, output: bytearray, limit: int,
) -> bool:
    truncated = False
    while chunk := await stream.read(8192):
        remaining = max(0, limit - len(output))
        output.extend(chunk[:remaining])
        truncated |= len(chunk) > remaining
    return truncated


# 等待主进程退出而不等待后代继承的输出管道，随后才能主动清理后台子进程
async def _wait_exit(proc: asyncio.subprocess.Process) -> None:
    while proc.returncode is None:
        await asyncio.sleep(0.02)


# 执行已准备的沙盒请求，并在超时、取消或正常结束时回收进程组
async def spawn(request: SandboxExecRequest) -> SandboxExecResult:
    if request.sandbox == SandboxType.WINDOWS_MXC:
        if sys.platform != "win32":
            raise UnsupportedSandboxError("MXC BaseContainer execution requires Windows")
        from .windows.launcher import execute

        return await execute(request)
    if sys.platform not in ("linux", "darwin"):
        raise UnsupportedSandboxError("Sandbox execution requires Linux or macOS")
    try:
        proc = await asyncio.create_subprocess_exec(
            *request.argv, cwd=request.cwd, env=request.env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True, close_fds=True,
        )
    except OSError as exc:
        raise SandboxSetupError(f"Cannot start sandbox: {exc}") from exc
    assert proc.stdout is not None
    output = bytearray()
    reader = asyncio.create_task(_read_output(proc.stdout, output, request.output_limit))
    timed_out = False
    truncated = False
    try:
        async with asyncio.timeout(request.timeout):
            await _wait_exit(proc)
            await _stop(proc)
            truncated = await asyncio.shield(reader)
    except TimeoutError:
        timed_out = True
    finally:
        # shield 保证外层工具超时取消时，清理任务仍然完成
        cleanup = asyncio.create_task(_stop(proc))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
        finally:
            if not reader.done():
                reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                truncated = await reader
    text = output.decode("utf-8", errors="replace")
    code = proc.returncode if proc.returncode is not None else -1
    if not timed_out and is_setup_failure(request.sandbox, code, text):
        raise SandboxSetupError(text.strip())
    return SandboxExecResult(
        code, text, timed_out, truncated, is_suspected_denial(code, text),
    )
