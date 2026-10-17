from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class NetworkPolicy(StrEnum):
    DENY = "deny"
    ALLOW = "allow"


class SandboxType(StrEnum):
    LINUX = "linux"
    MACOS = "macos"
    WINDOWS_MXC = "windows_mxc"


@dataclass(frozen=True)
class WindowsSandboxConfig:
    mxc_executable: Path | None = None
    network: NetworkPolicy = NetworkPolicy.DENY
    readonly_paths: tuple[Path, ...] = ()
    readwrite_paths: tuple[Path, ...] = ()
    denied_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class FileSystemPolicy:
    read_roots: tuple[Path, ...] = (Path("/"),)
    write_roots: tuple[Path, ...] = ()
    protected_paths: tuple[Path, ...] = ()
    deny_paths: tuple[Path, ...] = ()
    include_platform_defaults: bool = True


@dataclass(frozen=True)
class SandboxPolicy:
    file_system: FileSystemPolicy = field(default_factory=FileSystemPolicy)
    network: NetworkPolicy = NetworkPolicy.DENY


@dataclass(frozen=True)
class SandboxCommand:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = 60.0
    output_limit: int = 64 * 1024


@dataclass(frozen=True)
class SandboxExecRequest:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    sandbox: SandboxType
    timeout: float = 60.0
    output_limit: int = 64 * 1024
    path_locks: tuple[Path, ...] = ()


@dataclass(frozen=True)
class SandboxExecResult:
    returncode: int
    output: str
    timed_out: bool = False
    truncated: bool = False
    suspected_denial: bool = False
    stdout: str = ""
    stderr: str = ""
