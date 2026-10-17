from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kama_claude.core.config import KamaConfig, _apply_env, _apply_toml
from kama_claude.core.sandbox import (
    FileSystemPolicy,
    NetworkPolicy,
    SandboxCommand,
    SandboxPolicy,
    SandboxPolicyError,
    SandboxUnavailableError,
)
from kama_claude.core.sandbox.windows.command import (
    build_command,
    build_config,
    process_environment,
    validate_command_length,
)
from kama_claude.core.sandbox.windows.launcher import backend_error, validate_probe
from kama_claude.core.sandbox.windows.policy import filesystem_config, validate_windows_path
from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.builtin.bash import BashTool

ENV = {"SystemRoot": r"C:\Windows", "LOCALAPPDATA": r"C:\Users\tester\AppData\Local"}


def test_one_shot_json_roundtrip(tmp_path: Path) -> None:
    command = SandboxCommand(("program.exe", "a b", 'a"b', "中文"), tmp_path, ENV)
    policy = SandboxPolicy(FileSystemPolicy(read_roots=(tmp_path,)))
    argv = build_command(command, policy, tmp_path / "wxc-exec.exe")
    config = json.loads(base64.b64decode(argv[2]))
    assert config["version"] == "1.0.0"
    assert config["containment"] == "processcontainer"
    assert config["lifecycle"] == {"destroyOnExit": True, "preservePolicy": False}
    assert "sandboxId" not in config and "phase" not in config
    assert config["fallback"] == {"allowDaclMutation": False}
    assert config["process"]["cwd"] == str(tmp_path)
    assert '"a b"' in config["process"]["commandLine"]
    assert config["containerId"] != build_config(command, policy)["containerId"]


@pytest.mark.parametrize("mode", list(NetworkPolicy))
def test_directional_network_contract(tmp_path: Path, mode: NetworkPolicy) -> None:
    cfg = build_config(SandboxCommand(("echo",), tmp_path, ENV), SandboxPolicy(network=mode))
    assert cfg["network"] == {
        "egress": {"default": mode.value},
        "ingress": {"default": "deny", "hostLoopback": "deny"},
    }


def test_filesystem_restrictions_survive_mapping(tmp_path: Path) -> None:
    policy = SandboxPolicy(FileSystemPolicy(
        read_roots=(tmp_path / "read",), write_roots=(tmp_path,),
        protected_paths=(tmp_path / ".git",), deny_paths=(tmp_path / "secret",),
    ))
    cfg = filesystem_config(policy)
    assert str(tmp_path / ".git") in cfg["readonlyPaths"]
    assert cfg["deniedPaths"] == [str(tmp_path / "secret")]
    conflicting = replace(policy, file_system=replace(
        policy.file_system, write_roots=(tmp_path / ".git" / "objects",),
    ))
    with pytest.raises(SandboxPolicyError, match="overlaps"):
        filesystem_config(conflicting)


@pytest.mark.parametrize("path", [
    r"\\server\share", r"\\?\C:\data", r"\\.\NUL", "C:relative",
    "C:\\work\\file:stream", "C:\\work\\trailing.", "C:\\work\\trailing ",
    r"C:\work\NUL.txt", r"C:\work\a*b",
])
def test_ambiguous_windows_paths_rejected(path: str) -> None:
    with pytest.raises(SandboxPolicyError):
        validate_windows_path(path)


def test_environment_does_not_inherit_secrets_or_startup_hooks() -> None:
    env = process_environment({**ENV, "ANTHROPIC_API_KEY": "secret", "DOTNET_STARTUP_HOOKS": "x",
                               "MXC_FORCE_TIER": "appcontainer-dacl", "PATH": "runtime"})
    assert env == {"SYSTEMROOT": ENV["SystemRoot"], "LOCALAPPDATA": ENV["LOCALAPPDATA"],
                   "PATH": "runtime"}
    with pytest.raises(SandboxPolicyError, match="SYSTEMROOT"):
        process_environment({})


def test_oversized_config_fails_without_stdin_or_file_fallback() -> None:
    with pytest.raises(SandboxPolicyError, match="command-line"):
        validate_command_length(("wxc-exec.exe", "--config-base64", "a" * 32767))


@pytest.mark.parametrize("probe", [
    {}, [], {"tier": "appcontainer-dacl"},
    {"tier": "base-container", "needsDaclAugmentation": True},
    {"tier": "base-container", "needsDaclAugmentation": False, "probes": {"bfsCompiledIn": True}},
])
def test_probe_rejects_fallbacks(probe: object) -> None:
    with pytest.raises(SandboxUnavailableError):
        validate_probe(json.dumps(probe))


def test_probe_accepts_only_native_basecontainer() -> None:
    validate_probe(json.dumps({"tier": "base-container", "needsDaclAugmentation": False,
                               "probes": {"bfsCompiledIn": False}}))


def test_error_envelope_after_diagnostic() -> None:
    assert backend_error('diagnostic{"error":{"code":"backend_error","message":"failed"}}') == (
        "backend_error", "failed",
    )
    assert backend_error("ordinary command stderr") is None


def test_nested_write_cannot_reopen_denied_subtree(tmp_path: Path) -> None:
    with pytest.raises(SandboxPolicyError, match="denied path"):
        filesystem_config(SandboxPolicy(FileSystemPolicy(
            write_roots=(tmp_path / "child",), deny_paths=(tmp_path,),
        )))


def test_root_and_nested_agents_inherit_windows_configuration(tmp_path: Path) -> None:
    from kama_claude.core.events.bus import EventBus
    from kama_claude.core.runner import AgentRunner
    from kama_claude.core.subagent.tool import SpawnAgentTool
    from kama_claude.core.task.manager import TaskManager

    config = KamaConfig()
    config.sandbox.windows = replace(config.sandbox.windows, network=NetworkPolicy.ALLOW)
    runner = AgentRunner(config, runs_dir=tmp_path)
    registry = runner._build_registry(TaskManager(tmp_path), provider=AsyncMock(),
                                      bus=EventBus(), run_id="root")
    for depth in range(3):
        bash = registry.get("bash")
        assert isinstance(bash, BashTool)
        assert bash._windows is config.sandbox.windows
        if depth < 2:
            agent = registry.get("spawn_agent")
            assert isinstance(agent, SpawnAgentTool)
            registry = agent._build_child_registry(EventBus(), f"child-{depth}", None)


@pytest.mark.asyncio
async def test_invalid_probe_never_runs_workload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kama_claude.core.sandbox import SandboxExecRequest, SandboxExecResult, SandboxType
    from kama_claude.core.sandbox.windows.launcher import _execute_locked
    from kama_claude.core.sandbox.windows.process import CapturedProcess

    run = AsyncMock(return_value=CapturedProcess(
        SandboxExecResult(0, "", stdout='{"tier":"appcontainer-dacl"}'), "",
    ))
    monkeypatch.setattr("kama_claude.core.sandbox.windows.process.run_process", run)
    request = SandboxExecRequest((str(tmp_path / "mxc.exe"), "--config-base64", "payload"),
                                 tmp_path, {}, SandboxType.WINDOWS_MXC)
    with pytest.raises(SandboxUnavailableError):
        await _execute_locked(request)
    run.assert_awaited_once()
    assert run.call_args.args[0][-1] == "--probe"


def test_windows_config_overlay_and_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = KamaConfig()
    _apply_toml(config, {"sandbox": {"windows": {"network": "allow", "readonly_paths": ["data"]}}})
    _apply_toml(config, {"sandbox": {"windows": {"denied_paths": ["secret"]}}})
    monkeypatch.setenv("KAMA_MXC_EXECUTABLE", str(tmp_path / "wxc-exec.exe"))
    _apply_env(config)
    assert config.sandbox.windows.network == NetworkPolicy.ALLOW
    assert config.sandbox.windows.readonly_paths == (Path("data"),)
    assert config.sandbox.windows.denied_paths == (Path("secret"),)
    assert config.sandbox.windows.mxc_executable == tmp_path / "wxc-exec.exe"


@pytest.mark.parametrize("windows", [{"network": "unrestricted"}, {"readonly_paths": "C:\\"},
                                     {"unknown": True}, {"mxc_executable": 42}])
def test_invalid_windows_config(windows: object) -> None:
    with pytest.raises(SystemExit):
        _apply_toml(KamaConfig(), {"sandbox": {"windows": windows}})


@pytest.mark.asyncio
async def test_permission_denial_does_not_start_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    from kama_claude.core.events.bus import EventBus
    from kama_claude.core.llm.types import ToolCallBlock
    from kama_claude.core.tools.invocation import invoke_tool
    from kama_claude.core.tools.registry import ToolRegistry

    tool = BashTool()
    invoked = AsyncMock(return_value=ToolResult("unexpected"))
    monkeypatch.setattr(tool, "invoke", invoked)
    registry = ToolRegistry()
    registry.register(tool)
    permissions = AsyncMock()
    permissions.check_and_wait.return_value = (False, "deny")
    result = await invoke_tool(registry, ToolCallBlock(id="t", name="bash", input={"command": "x"}),
                               EventBus(), "r", permission_manager=permissions)
    assert result.is_error
    invoked.assert_not_awaited()
