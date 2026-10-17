from __future__ import annotations

import ctypes
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes as wt
from pathlib import Path

from ..errors import SandboxPolicyError, UnsupportedSandboxError


@contextmanager
def lock_paths(paths: tuple[Path, ...]) -> Iterator[None]:
    """Pin path components without FILE_SHARE_DELETE, before validating/launching MXC.

    PSEC read-only rules alone do not prevent renaming a writable ancestor. Holding
    these host-owned, non-inherited handles also prevents replacing roots by junctions.
    """
    if sys.platform != "win32":
        raise UnsupportedSandboxError("Windows path locks require Windows")
    import _winapi

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetFileInformationByHandleEx.argtypes = [
        wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
    ]
    kernel.GetFileInformationByHandleEx.restype = wt.BOOL
    kernel.GetFinalPathNameByHandleW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD, wt.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wt.DWORD
    components = {part for path in paths for part in (path, *path.parents)}
    handles: list[int] = []
    try:
        for path in sorted(components, key=lambda p: (len(p.parts), str(p))):
            try:
                # Access=0 only queries metadata; sharing R/W permits ordinary file operations.
                handle = _winapi.CreateFile(str(path), 0, 3, 0, 3, 0x02200000, 0)
                handles.append(handle)
                attributes = (wt.DWORD * 2)()
                if not kernel.GetFileInformationByHandleEx(
                    handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if attributes[0] & 0x400:
                    raise SandboxPolicyError(f"Sandbox path became a reparse point: {path}")
                buffer = ctypes.create_unicode_buffer(32768)
                count = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
                if not count or count >= len(buffer):
                    raise SandboxPolicyError(f"Cannot verify sandbox path identity: {path}")
                actual = buffer.value.removeprefix("\\\\?\\")
                if Path(actual) != path:
                    raise SandboxPolicyError(f"Sandbox path identity changed: {path}")
            except OSError as exc:
                raise SandboxPolicyError(f"Cannot pin sandbox path: {path}") from exc
        yield
    finally:
        for handle in reversed(handles):
            _winapi.CloseHandle(handle)
