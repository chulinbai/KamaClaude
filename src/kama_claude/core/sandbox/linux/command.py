from __future__ import annotations

import json
import sys
from pathlib import Path

from ..models import SandboxCommand, SandboxPolicy
from .launcher import find_bwrap


# 将最终策略传给独立的标准库辅助进程，避免在主进程安装 seccomp
def build_command(command: SandboxCommand, policy: SandboxPolicy) -> tuple[str, ...]:
    launcher = find_bwrap()
    fs = policy.file_system
    payload = {
        "bwrap": str(launcher), "argv": command.argv, "cwd": str(command.cwd),
        "network": policy.network.value,
        "read_roots": [str(p) for p in fs.read_roots],
        "write_roots": [str(p) for p in fs.write_roots],
        "protected_paths": [str(p) for p in fs.protected_paths],
        "deny_paths": [str(p) for p in fs.deny_paths],
        "include_platform_defaults": fs.include_platform_defaults,
    }
    entry = Path(__file__).with_name("__main__.py").resolve()
    return (sys.executable, "-I", "-S", str(entry), "--outer", json.dumps(payload))
