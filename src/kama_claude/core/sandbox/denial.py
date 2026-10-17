from __future__ import annotations

from .models import SandboxType

SETUP_ERROR_PREFIX = "KAMA_SANDBOX_SETUP_ERROR:"


# 仅对失败命令提示疑似沙盒拒绝，不据此授权重试或扩大权限
def is_suspected_denial(returncode: int, output: str) -> bool:
    if returncode == 0:
        return False
    lowered = output.lower()
    return any(message in lowered for message in (
        "operation not permitted", "permission denied", "read-only file system",
        "sandbox: deny", "bad system call",
    ))


# 区分后端初始化诊断和命令自身失败，初始化错误不得普通重试
def is_setup_failure(sandbox: SandboxType, returncode: int, output: str) -> bool:
    if returncode == 0:
        return False
    if SETUP_ERROR_PREFIX in output:
        return True
    prefix = "bwrap:" if sandbox == SandboxType.LINUX else "sandbox-exec:"
    return any(line.startswith(prefix) for line in output.splitlines())
