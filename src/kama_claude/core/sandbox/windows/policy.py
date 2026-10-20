from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path, PureWindowsPath

from ..errors import SandboxPolicyError
from ..models import FileSystemPolicy, SandboxPolicy, WindowsSandboxConfig


def validate_windows_path(text: str) -> None:
    """Only ordinary local drive paths: reject aliases rather than rewriting them."""
    path = PureWindowsPath(text)
    if text.startswith(("\\\\", "//")) or (path.drive and not path.root):
        raise SandboxPolicyError(f"Unsupported Windows sandbox path: {text!r}")
    for part in path.parts[1:] if path.anchor else path.parts:
        if part in (".", ".."):
            continue
        if (part.endswith((".", " ")) or any(c in part for c in '<>:"|?*')
                or PureWindowsPath(part).is_reserved()):
            raise SandboxPolicyError(f"Ambiguous Windows sandbox path: {text!r}")


def workspace_policy(workspace: Path, config: WindowsSandboxConfig) -> SandboxPolicy:
    return SandboxPolicy(FileSystemPolicy(
        read_roots=config.readonly_paths,
        write_roots=(workspace, *config.readwrite_paths),
        deny_paths=config.denied_paths,
    ), config.network)


def add_runtime_paths(policy: SandboxPolicy, cwd: Path, executable: Path) -> SandboxPolicy:
    fs = policy.file_system
    reads = fs.read_roots
    if fs.include_platform_defaults:
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        # Read grants on volume roots expose unrelated files on current Windows builds.
        # Even enumerate-only volume grants leak names outside approved directories.
        # Explicit cwd and the runtime paths below are sufficient for PowerShell.
        reads += (system_root, Path(sys.prefix), Path(sys.base_prefix),
                  Path(sys.executable).parent)
    return replace(policy, file_system=replace(
        fs, read_roots=reads,
        protected_paths=fs.protected_paths + (executable.parent,),
    ))


def filesystem_config(policy: SandboxPolicy) -> dict[str, list[str]]:
    fs = policy.file_system
    # Read-only restrictions must not be undone by a nested explicit write grant.
    for write in fs.write_roots:
        if any(write.is_relative_to(protected) for protected in fs.protected_paths):
            raise SandboxPolicyError(f"Write grant overlaps protected runtime/path: {write}")
        if any(write.is_relative_to(denied) for denied in fs.deny_paths):
            raise SandboxPolicyError(f"Write grant overlaps denied path: {write}")
    # 公共策略中的读取授权可与同一路径的写入授权重合；MXC 的只读规则会拦截写入。
    # 精确重复项由 readwritePaths 提供读写权限，显式保护目录仍必须保持只读。
    reads = tuple(path for path in fs.read_roots if path not in fs.write_roots)
    return {
        "readonlyPaths": list(dict.fromkeys(map(str, (*reads, *fs.protected_paths)))),
        "readwritePaths": list(map(str, fs.write_roots)),
        "deniedPaths": list(map(str, fs.deny_paths)),
    }
