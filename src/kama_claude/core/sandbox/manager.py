from __future__ import annotations

import math
import sys
from dataclasses import replace
from pathlib import Path

from .errors import SandboxPolicyError, UnsupportedSandboxError
from .models import (
    SandboxCommand,
    SandboxExecRequest,
    SandboxPolicy,
    SandboxType,
    WindowsSandboxConfig,
)
from .policy import clean_environment, normalize_path, normalize_policy, protect_runtime


class SandboxManager:
    def __init__(self, windows: WindowsSandboxConfig | None = None) -> None:
        self.windows = windows or WindowsSandboxConfig()

    # 仅选择当前真实平台，避免在不支持的平台回退为普通命令
    def prepare(self, command: SandboxCommand, policy: SandboxPolicy) -> SandboxExecRequest:
        if sys.platform not in ("linux", "darwin", "win32"):
            raise UnsupportedSandboxError(f"Bash sandbox is unsupported on {sys.platform}")
        if not command.argv or any("\0" in arg for arg in command.argv):
            raise SandboxPolicyError("Sandbox command must contain a valid argv")
        if any(not key or "=" in key or "\0" in key or "\0" in value
               for key, value in command.env.items()):
            raise SandboxPolicyError("Sandbox environment contains an invalid key or value")
        if not math.isfinite(command.timeout) or command.timeout <= 0 or command.output_limit < 1:
            raise SandboxPolicyError("Sandbox timeout and output limit must be positive")
        cwd = normalize_path(command.cwd, Path.cwd())
        if not cwd.is_dir():
            raise SandboxPolicyError(f"Sandbox cwd is not a directory: {cwd}")
        command = replace(command, cwd=cwd, env=clean_environment(command.env))
        # 防止可写工作区中的 editable 安装被命令改写后，在下一次辅助进程启动时执行
        policy = protect_runtime(policy)
        if sys.platform == "win32":
            from .windows.command import build_command as build_windows_command
            from .windows.command import launcher_environment
            from .windows.launcher import find_mxc
            from .windows.policy import add_runtime_paths

            executable = find_mxc(self.windows)
            audit_roots = tuple(normalize_path(p, cwd) for p in (
                *policy.file_system.read_roots, *policy.file_system.write_roots,
            ))
            policy = normalize_policy(add_runtime_paths(policy, cwd, executable), cwd)
            return SandboxExecRequest(
                build_windows_command(command, policy, executable), cwd,
                launcher_environment(command.env), SandboxType.WINDOWS_MXC,
                command.timeout, command.output_limit,
                path_locks=(cwd, executable, *policy.file_system.read_roots,
                            *policy.file_system.write_roots, *policy.file_system.protected_paths,
                            *policy.file_system.deny_paths),
                hardlink_audit_roots=audit_roots,
            )
        policy = normalize_policy(policy, cwd)
        if sys.platform == "linux":
            from .linux.command import build_command

            argv = build_command(command, policy)
            sandbox = SandboxType.LINUX
        else:
            from .macos.seatbelt import build_command

            argv = build_command(command, policy)
            sandbox = SandboxType.MACOS
        return SandboxExecRequest(
            argv, cwd, command.env, sandbox, command.timeout, command.output_limit,
        )
