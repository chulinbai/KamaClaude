from __future__ import annotations

import json
import platform
import struct
from dataclasses import replace
from pathlib import Path

from ..denial import is_suspected_denial
from ..errors import (
    SandboxExecutionError,
    SandboxPolicyError,
    SandboxSetupError,
    SandboxUnavailableError,
)
from ..models import SandboxExecRequest, SandboxExecResult, WindowsSandboxConfig
from ..policy import normalize_path


def find_mxc(config: WindowsSandboxConfig) -> Path:
    architecture = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        architecture,
    )
    if arch is None:
        raise SandboxUnavailableError(f"Unsupported Windows architecture: {architecture}")
    configured = config.mxc_executable
    if configured is not None and not configured.is_absolute():
        raise SandboxPolicyError("sandbox.windows.mxc_executable must be an absolute path")
    path = configured or Path(__file__).resolve().parents[5] / "bin/mxc" / arch / "wxc-exec.exe"
    path = normalize_path(path, Path.cwd())
    if not path.is_file():
        raise SandboxUnavailableError(f"MXC executable is missing: {path}")
    try:
        with path.open("rb") as file:
            header = file.read(64)
            if len(header) < 64 or header[:2] != b"MZ":
                raise ValueError("not a PE executable")
            file.seek(struct.unpack_from("<I", header, 60)[0])
            signature = file.read(6)
            expected = 0x8664 if arch == "x64" else 0xAA64
            if signature[:4] != b"PE\0\0" or len(signature) != 6:
                raise ValueError("invalid PE signature")
            if struct.unpack_from("<H", signature, 4)[0] != expected:
                raise ValueError("executable architecture does not match the host")
    except (OSError, ValueError) as exc:
        raise SandboxUnavailableError(f"Invalid MXC executable: {exc}") from exc
    return path


def validate_probe(output: str) -> None:
    try:
        probe = json.loads(output)
        if not isinstance(probe, dict):
            raise ValueError("probe is not an object")
        facts = probe.get("probes", {})
        if (probe.get("tier") != "base-container"
                or probe.get("needsDaclAugmentation") is not False
                or not isinstance(facts, dict) or facts.get("bfsCompiledIn") is not False):
            raise ValueError("BaseContainer without AppContainer/BFS/DACL fallback is required")
    except (ValueError, TypeError) as exc:
        raise SandboxUnavailableError(f"MXC capability check failed: {exc}") from exc


def backend_error(stderr: str) -> tuple[str, str] | None:
    # The CLI may append its envelope directly after a diagnostic, without a newline.
    decoder = json.JSONDecoder()
    position = stderr.rfind('{"error":')
    if position < 0:
        return None
    try:
        value, _ = decoder.raw_decode(stderr[position:])
        error = value["error"]
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return error["code"], str(error.get("message", "MXC execution failed"))
    except (ValueError, KeyError, TypeError):
        pass
    return None


async def execute(request: SandboxExecRequest) -> SandboxExecResult:
    from .path_locks import lock_paths

    with lock_paths(request.path_locks):
        return await _execute_locked(request)


async def _execute_locked(request: SandboxExecRequest) -> SandboxExecResult:
    from .process import run_process

    # Launch from the protected binary directory, not an attacker-writable working directory.
    cwd = Path(request.argv[0]).parent
    probe = await run_process((request.argv[0], "--probe"), cwd, request.env, 10, 65536)
    if probe.result.returncode != 0 or probe.result.timed_out or probe.result.truncated:
        raise SandboxUnavailableError("MXC capability probe failed or timed out")
    validate_probe(probe.result.stdout)
    checked = await run_process((*request.argv, "--dry-run"), cwd, request.env, 10, 65536)
    if (checked.result.returncode != 0 or checked.result.timed_out or checked.result.truncated
            or "selected isolation tier: base-container" not in checked.result.output):
        raise SandboxPolicyError("MXC cannot enforce this configuration with BaseContainer")
    captured = await run_process(
        request.argv, cwd, request.env, request.timeout, request.output_limit,
    )
    result = captured.result
    error = backend_error(captured.stderr_tail) if result.returncode != 0 else None
    if error and not result.timed_out:
        code, message = error
        if "timed out" in message.lower() or code == "timeout":
            return replace(result, timed_out=True)
        if code in ("backend_unavailable", "unsupported_containment"):
            raise SandboxUnavailableError(message)
        if code in ("policy_validation", "malformed_request", "invalid_config"):
            raise SandboxPolicyError(message)
        if code in ("spawn_failed", "setup_failed"):
            raise SandboxSetupError(message)
        raise SandboxExecutionError(message)
    return replace(result, suspected_denial=is_suspected_denial(result.returncode, result.output))
