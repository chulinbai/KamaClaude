"""Own MXC's entire process tree before allowing the launcher to execute.

Anonymous pipes are polled with PeekNamedPipe: no uninterruptible reader threads,
and no reliance on asyncio's private Windows subprocess transports.
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes as wt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import SandboxCleanupError, SandboxSetupError, UnsupportedSandboxError
from ..models import SandboxExecResult
from .command import validate_command_length

if sys.platform == "win32":
    import _winapi


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("per_process", ctypes.c_int64), ("per_job", ctypes.c_int64),
        ("flags", wt.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
        ("active_limit", wt.DWORD), ("affinity", ctypes.c_size_t),
        ("priority", wt.DWORD), ("scheduling", wt.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t),
    ]


if sys.platform == "win32":
    def _kernel() -> Any:
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wt.LPCWSTR], wt.HANDLE),
            "SetInformationJobObject": (
                [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD], wt.BOOL,
            ),
            "AssignProcessToJobObject": ([wt.HANDLE, wt.HANDLE], wt.BOOL),
            "TerminateJobObject": ([wt.HANDLE, wt.UINT], wt.BOOL),
            "ResumeThread": ([wt.HANDLE], wt.DWORD),
        }
        for name, (args, result) in signatures.items():
            function = getattr(api, name)
            function.argtypes = args
            function.restype = result
        return api


    class _Process:
        def __init__(self, argv: tuple[str, ...], cwd: Path, env: dict[str, str]) -> None:
            validate_command_length(argv)
            self.api = _kernel()
            self.handles: set[int] = set()
            self.job = 0
            self.process = 0
            self.stdout = 0
            self.stderr = 0
            thread = 0
            try:
                self.job = self.api.CreateJobObjectW(None, None)
                if not self.job:
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handles.add(self.job)
                limits = _ExtendedLimits()
                limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
                if not self.api.SetInformationJobObject(
                    self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.stdout, stdout_write = _winapi.CreatePipe(None, 0)
                self.handles.update((self.stdout, stdout_write))
                self.stderr, stderr_write = _winapi.CreatePipe(None, 0)
                self.handles.update((self.stderr, stderr_write))
                null = _winapi.CreateFile("NUL", 0x80000000, 3, 0, 3, 0, 0)
                self.handles.add(null)
                inherited = [null, stdout_write, stderr_write]
                for handle in inherited:
                    os.set_handle_inheritable(handle, True)
                startup = subprocess.STARTUPINFO()
                startup.dwFlags = subprocess.STARTF_USESTDHANDLES
                startup.hStdInput = null
                startup.hStdOutput = stdout_write
                startup.hStdError = stderr_write
                startup.lpAttributeList = {"handle_list": inherited}
                flags = 0x4 | subprocess.CREATE_NO_WINDOW | 0x400  # suspended, Unicode environment
                self.process, thread, self.pid, _ = _winapi.CreateProcess(
                    argv[0], subprocess.list2cmdline(argv), None, None, True,
                    flags, env, str(cwd), startup,
                )
                self.handles.update((self.process, thread))
                if not self.api.AssignProcessToJobObject(self.job, self.process):
                    raise ctypes.WinError(ctypes.get_last_error())
                if self.api.ResumeThread(thread) == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
                for handle in (*inherited, thread):
                    self._close(handle)
            except BaseException:
                # Assignment failure leaves a suspended process outside the job.
                # Terminate it explicitly before releasing its handles.
                try:
                    if self.process:
                        _winapi.TerminateProcess(self.process, 1)
                        _winapi.WaitForSingleObject(self.process, 5000)
                finally:
                    self.close()
                raise

        def _close(self, handle: int) -> None:
            if handle in self.handles:
                _winapi.CloseHandle(handle)
                self.handles.remove(handle)

        def close(self) -> None:
            # Job first: even a launcher failure must close every descendant's pipe writer.
            self._close(self.job)
            for handle in tuple(self.handles):
                self._close(handle)

        def stop(self) -> None:
            if not self.api.TerminateJobObject(self.job, 1):
                raise SandboxCleanupError("Cannot terminate Windows sandbox job")

        def poll(self) -> int | None:
            if _winapi.WaitForSingleObject(self.process, 0) == 0:
                return int(_winapi.GetExitCodeProcess(self.process))
            return None

        def read(self, handle: int) -> bytes | None:
            try:
                available = _winapi.PeekNamedPipe(handle, 0)[-2]
                if not available:
                    return b""
                data, _ = _winapi.ReadFile(handle, min(available, 8192))
                return bytes(data)
            except OSError as exc:
                if exc.winerror in (109, 232):  # broken/disconnected pipe => EOF
                    return None
                raise


else:
    class _Process:
        def __init__(self, argv: tuple[str, ...], cwd: Path, env: dict[str, str]) -> None:
            raise UnsupportedSandboxError("Windows process management requires Windows")


@dataclass
class CapturedProcess:
    result: SandboxExecResult
    stderr_tail: str


async def run_process(
    argv: tuple[str, ...], cwd: Path, env: dict[str, str], timeout: float, limit: int,
) -> CapturedProcess:
    if sys.platform != "win32":
        raise UnsupportedSandboxError("Windows process management requires Windows")
    try:
        proc = _Process(argv, cwd, env)
    except OSError as exc:
        # Do not include argv: it contains a Base64 environment/configuration payload.
        raise SandboxSetupError(f"Cannot launch MXC (Windows error {exc.winerror})") from exc
    output, stdout, stderr, tail = bytearray(), bytearray(), bytearray(), bytearray()
    truncated = False

    async def drain(handle: int, destination: bytearray, is_stderr: bool) -> None:
        nonlocal truncated
        while True:
            chunk = proc.read(handle)
            if chunk is None:
                return
            if chunk:
                kept = chunk[:max(0, limit - len(output))]
                destination.extend(kept)
                output.extend(kept)
                truncated |= len(kept) != len(chunk)
                if is_stderr:
                    tail.extend(chunk)
                    del tail[:-8192]
            await asyncio.sleep(0.005)

    readers = [asyncio.create_task(drain(proc.stdout, stdout, False)),
               asyncio.create_task(drain(proc.stderr, stderr, True))]
    timed_out = False
    code = -1
    cancelled: asyncio.CancelledError | None = None

    async def cleanup() -> None:
        try:
            proc.stop()
            async with asyncio.timeout(5):
                while proc.poll() is None:
                    await asyncio.sleep(0.01)
                await asyncio.gather(*readers)
        except (OSError, TimeoutError) as exc:
            raise SandboxCleanupError("Windows sandbox process/pipe cleanup failed") from exc
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            proc.close()

    try:
        async with asyncio.timeout(timeout):
            while (status := proc.poll()) is None:
                for reader in readers:
                    if reader.done():
                        reader.result()
                await asyncio.sleep(0.01)
            code = status
    except TimeoutError:
        timed_out = True
    except asyncio.CancelledError as exc:
        cancelled = exc
    finally:
        task = asyncio.create_task(cleanup())
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                cancelled = exc
    if cancelled is not None:
        raise cancelled
    return CapturedProcess(SandboxExecResult(
        code, output.decode("utf-8", errors="replace"), timed_out, truncated,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    ), tail.decode("utf-8", errors="replace"))
