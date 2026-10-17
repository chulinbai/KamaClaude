from __future__ import annotations

import base64
import json
import os
import subprocess
import uuid
from pathlib import Path

from ..errors import SandboxPolicyError, SandboxUnavailableError
from ..models import NetworkPolicy, SandboxCommand, SandboxPolicy
from .policy import filesystem_config

_ENV_KEYS = frozenset({
    "SYSTEMROOT", "WINDIR", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "COMSPEC",
    "PATH", "PATHEXT", "TEMP", "TMP", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
})


def process_environment(env: dict[str, str]) -> dict[str, str]:
    result = {key.upper(): value for key, value in env.items() if key.upper() in _ENV_KEYS}
    for required in ("SYSTEMROOT", "LOCALAPPDATA"):
        if not result.get(required):
            raise SandboxPolicyError(f"Windows sandbox environment requires {required}")
    return result


def launcher_environment(env: dict[str, str]) -> dict[str, str]:
    result = process_environment(env)
    root = Path(result["SYSTEMROOT"])
    result["PATH"] = str(root / "System32") + os.pathsep + str(root)
    return result


def powershell_command(script: str) -> tuple[str, ...]:
    if "\0" in script:
        raise SandboxPolicyError("PowerShell command contains NUL")
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    if not executable.is_file():
        raise SandboxUnavailableError("System Windows PowerShell is unavailable")
    prelude = (
        "$ProgressPreference='SilentlyContinue'; "
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
        "$OutputEncoding=[Console]::OutputEncoding; "
        "Set-Location -LiteralPath ([Environment]::CurrentDirectory) -ErrorAction Stop;\n"
    )
    encoded = base64.b64encode((prelude + script).encode("utf-16le")).decode("ascii")
    return (str(executable), "-NoLogo", "-NoProfile", "-NonInteractive",
            "-OutputFormat", "Text", "-EncodedCommand", encoded)


def build_config(command: SandboxCommand, policy: SandboxPolicy) -> dict[str, object]:
    env = process_environment(command.env)
    return {
        "version": "1.0.0",
        "containment": "processcontainer",
        "containerId": "kama-" + uuid.uuid4().hex,
        "lifecycle": {"destroyOnExit": True, "preservePolicy": False},
        "process": {
            "commandLine": subprocess.list2cmdline(command.argv),
            "cwd": str(command.cwd), "env": [f"{k}={v}" for k, v in sorted(env.items())],
            "timeout": max(1, int(command.timeout * 1000)),
        },
        "filesystem": filesystem_config(policy),
        "fallback": {"allowDaclMutation": False},
        "network": {
            "egress": {"default": "allow" if policy.network == NetworkPolicy.ALLOW else "deny"},
            "ingress": {"default": "deny", "hostLoopback": "deny"},
        },
        # PowerShell needs Win32k and registry reads to initialize. These do not grant writes.
        "ui": {"disable": False, "clipboard": "none", "injection": False},
        "processContainer": {"capabilities": ["registryRead"]},
        "telemetry": {"enabled": False},
    }


def validate_command_length(argv: tuple[str, ...]) -> None:
    length = len(subprocess.list2cmdline(argv).encode("utf-16le")) // 2 + 1
    if length > 32767:
        raise SandboxPolicyError("MXC configuration exceeds the Windows command-line limit")


def build_command(
    command: SandboxCommand, policy: SandboxPolicy, executable: Path,
) -> tuple[str, ...]:
    data = json.dumps(build_config(command, policy), ensure_ascii=False, separators=(",", ":"))
    payload = base64.b64encode(data.encode("utf-8")).decode("ascii")
    argv = (str(executable), "--config-base64", payload)
    validate_command_length((*argv, "--dry-run"))
    return argv
