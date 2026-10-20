from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from kama_claude.core.bus.events import PermissionRequestedEvent
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.paths import PathAccessController
from kama_claude.core.tools.builtin import BashTool, CreateDirTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry


# 功能：真实系统沙箱仅放行新建目标，不允许写入其父目录和相邻目录
# 设计：先经过真实路径审批创建目录，再启动平台沙箱验证三个不同位置的实际磁盘结果
@pytest.mark.sandbox_native
async def test_approved_directory_then_native_shell(tmp_path: Path, request: pytest.FixtureRequest) -> None:
    if sys.platform == "win32":
        if os.environ.get("KAMA_REQUIRE_WINDOWS_SANDBOX") != "1":
            pytest.skip("Set KAMA_REQUIRE_WINDOWS_SANDBOX=1 to exercise Windows MXC")
    else:
        request.getfixturevalue("native_sandbox")
    work = tmp_path / "project"
    work.mkdir()
    target = tmp_path / "test_file 中文"
    neighbor = tmp_path / "neighbor"
    neighbor.mkdir()
    access = PathAccessController(work)
    manager = PermissionManager(timeout_s=1)
    bus = EventBus()
    paths: list[str] = []

    # 只批准服务端展示的精确路径，后续 Shell 的普通工具请求单独放行
    async def approve(event: BaseModel) -> None:
        if isinstance(event, PermissionRequestedEvent):
            paths.extend(p.path for p in event.paths)
            assert manager.respond(event.tool_use_id,
                                   "allow_session" if event.paths else "allow_once", event.request_id)

    bus.subscribe(approve)
    registry = ToolRegistry()
    registry.register(CreateDirTool(access))
    registry.register(BashTool(access=access))
    created = await invoke_tool(registry, ToolCallBlock(
        id="create", name="create_dir", input={"path": str(target)},
    ), bus, "run", permission_manager=manager, session_id="session")
    assert not created.is_error, created.content
    assert target.is_dir()
    assert paths == [str(target)]
    ok = target / "ok.txt"
    bad_parent = tmp_path / "parent.txt"
    bad_neighbor = neighbor / "neighbor.txt"
    if sys.platform == "win32":
        # 将固定测试路径编码成 PowerShell 单引号字面量，避免命令拼接解释路径内容
        def quote(path: Path) -> str:
            return "'" + str(path).replace("'", "''") + "'"

        command = (
            f"$ErrorActionPreference='Stop'; [IO.File]::WriteAllText({quote(ok)},'ok'); "
            f"try {{ [IO.File]::WriteAllText({quote(bad_parent)},'bad'); exit 41 }} catch {{}}; "
            f"try {{ [IO.File]::WriteAllText({quote(bad_neighbor)},'bad'); exit 42 }} catch {{}}; "
            "'scope verified'"
        )
    else:
        command = (
            f"printf ok > {shlex.quote(str(ok))} || exit 40; "
            f"if printf bad > {shlex.quote(str(bad_parent))}; then exit 41; fi; "
            f"if printf bad > {shlex.quote(str(bad_neighbor))}; then exit 42; fi; "
            "echo 'scope verified'"
        )
    result = await invoke_tool(registry, ToolCallBlock(id="shell", name="bash", input={
        "command": command, "required_paths": [{"path": str(target), "access": "write"}],
    }), bus, "run", permission_manager=manager, session_id="session")
    assert not result.is_error, result.content
    assert "scope verified" in result.content
    assert ok.read_text() == "ok"
    assert not bad_parent.exists() and not bad_neighbor.exists()
    assert paths == [str(target)]
