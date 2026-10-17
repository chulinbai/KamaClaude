from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.sandbox import (
    SandboxCommand,
    SandboxError,
    SandboxExecResult,
    SandboxManager,
    SandboxPolicy,
    SandboxPolicyError,
    WindowsSandboxConfig,
    spawn,
    workspace_policy,
)
from kama_claude.core.sandbox.macos.seatbelt_scratch import process_scratch
from kama_claude.core.tools.base import BaseTool, ToolResult

_MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB
_DEFAULT_TIMEOUT = 60


class BashParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command: str
    timeout: int = Field(default=_DEFAULT_TIMEOUT, ge=1, le=120)


class BashTool(BaseTool):
    params_model = BashParams
    name = "bash"
    description = (
        "Execute a shell command and return its output (stdout + stderr combined). "
        "Non-interactive only — commands requiring user input will hang and time out. "
        "Prefer short, focused commands. Output is truncated at 64 KB."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Maximum seconds to wait (default {_DEFAULT_TIMEOUT}, max 120).",
            },
        },
        "required": ["command"],
    }

    # 固定工作区和沙盒策略，主 agent 与子 agent 默认使用同一执行边界
    def __init__(
        self, *, cwd: Path | None = None, policy: SandboxPolicy | None = None,
        manager: SandboxManager | None = None,
        windows: WindowsSandboxConfig | None = None,
    ) -> None:
        self._cwd = cwd or Path.cwd()
        self._policy = policy
        self._windows = windows or WindowsSandboxConfig()
        self._sandbox = manager or SandboxManager(self._windows)
        if sys.platform == "win32":
            self.execution_overhead_s = 35.0  # Two preflights plus bounded process cleanup.
            self.description = (
                "Execute a Windows PowerShell command in a one-shot BaseContainer sandbox. "
                "Use PowerShell syntax, not Bash. Non-interactive; stdin is closed. "
                "Returns stdout and stderr combined, truncated at 64 KB."
            )

    # 审批通过后在系统沙盒中执行命令，明确返回不可重试的沙盒错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = BashParams.model_validate(params)
        try:
            if sys.platform == "win32":
                from kama_claude.core.sandbox.windows.command import powershell_command
                from kama_claude.core.sandbox.windows.policy import workspace_policy as win_policy
                from kama_claude.core.sandbox.windows.scratch import process_scratch as win_scratch

                policy = self._policy or win_policy(self._cwd, self._windows)
                with win_scratch(policy, dict(os.environ)) as (scratch_policy, env):
                    command = SandboxCommand(
                        powershell_command(p.command), self._cwd, env, p.timeout, _MAX_OUTPUT_BYTES,
                    )
                    result = await spawn(self._sandbox.prepare(command, scratch_policy))
                return self._result(result, p.timeout)
            policy = self._policy or workspace_policy(self._cwd)
            command = SandboxCommand(
                ("/bin/sh", "-c", p.command), self._cwd, dict(os.environ),
                p.timeout, _MAX_OUTPUT_BYTES,
            )
            if sys.platform == "darwin":
                with process_scratch(policy, command.env) as (scratch_policy, env):
                    command = SandboxCommand(
                        command.argv, command.cwd, env, command.timeout, command.output_limit,
                    )
                    result = await spawn(self._sandbox.prepare(command, scratch_policy))
            else:
                result = await spawn(self._sandbox.prepare(command, policy))
        except SandboxError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type=exc.error_type)
        except OSError as exc:
            return ToolResult(
                content=f"Cannot prepare sandbox paths: {exc}", is_error=True,
                error_type=SandboxPolicyError.error_type,
            )

        return self._result(result, p.timeout)

    @staticmethod
    def _result(result: SandboxExecResult, timeout: int) -> ToolResult:
        output = result.output + ("\n[truncated]" if result.truncated else "")
        if result.timed_out:
            return ToolResult(
                content=f"[timeout after {timeout}s]\n{output}",
                is_error=True, error_type="timeout",
            )
        if result.returncode != 0:
            return ToolResult(
                content=f"[exit {result.returncode}]\n{output}",
                is_error=True,
                error_type="sandbox_denied" if result.suspected_denial else "command_failed",
            )
        return ToolResult(content=output or "[no output]")
