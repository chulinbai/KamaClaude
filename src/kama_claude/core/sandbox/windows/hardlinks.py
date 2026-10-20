"""Reject pre-existing hardlink aliases in user-granted filesystem trees.

BaseContainer authorizes paths, not all names of an NTFS file. An alias in a
writable tree can therefore modify a denied file elsewhere. Do not execute a
command against such a tree, or silently unlink/copy the user's files.
"""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from ..errors import SandboxPolicyError
from ..models import SandboxPolicy
from ..policy import protect_runtime


def audit_hardlinks(roots: tuple[Path, ...], executable: Path) -> None:
    # Trusted code dependencies are implicitly readable, separately write-protected,
    # and may legitimately use package-manager/Windows component-store hardlinks.
    trusted = (*protect_runtime(SandboxPolicy()).file_system.protected_paths, executable.parent)
    pending = list(set(roots))
    seen: set[Path] = set()
    deadline = time.monotonic() + 10
    while pending:
        path = pending.pop()
        if path in seen or any(path.is_relative_to(root) for root in trusted):
            continue
        seen.add(path)
        if len(seen) > 100_000 or time.monotonic() > deadline:
            raise SandboxPolicyError("Windows hardlink audit exceeds its size/time limit")
        try:
            # DirEntry.stat() on Windows may report zero for st_nlink: use os.stat.
            info = os.stat(path, follow_symlinks=False)
            if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                # Do not follow junctions. PSEC checks their destination at file-open time.
                continue
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise SandboxPolicyError(
                    f"Windows sandbox grant contains a hardlinked file: {path}",
                )
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    for entry in entries:
                        pending.append(Path(entry.path))
                        if len(pending) > 100_000:
                            raise SandboxPolicyError(
                                "Windows hardlink audit exceeds its size limit",
                            )
        except OSError as exc:
            raise SandboxPolicyError(f"Cannot audit Windows sandbox path: {path}") from exc
