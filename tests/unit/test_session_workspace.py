from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from kama_claude.core.bus.envelope import INVALID_PARAMS, HandlerError
from kama_claude.core.config import KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse
from kama_claude.core.runner import AgentRunner
from kama_claude.core.sandbox import FileSystemPolicy, SandboxPolicy, SandboxPolicyError
from kama_claude.core.sandbox.windows.policy import filesystem_config
from kama_claude.core.session.manager import SESSION_PROJECT_MISMATCH, SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.subagent.tool import SpawnAgentTool
from kama_claude.core.task.manager import TaskManager
from kama_claude.core.tools.builtin.bash import BashTool


# 读取授权与同一路径的写入授权合并；嵌套只读目录和运行保护不能因此丢失。
def test_windows_workspace_grants_do_not_cancel_writes(tmp_path: Path) -> None:
    readonly = tmp_path / "reference"
    protected = tmp_path / ".git"
    config = filesystem_config(SandboxPolicy(FileSystemPolicy(
        read_roots=(tmp_path, readonly), write_roots=(tmp_path,), protected_paths=(protected,),
    )))
    assert config["readwritePaths"] == [str(tmp_path)]
    assert config["readonlyPaths"] == [str(readonly), str(protected)]
    with pytest.raises(SandboxPolicyError):
        filesystem_config(SandboxPolicy(FileSystemPolicy(
            read_roots=(protected,), write_roots=(protected,), protected_paths=(protected,),
        )))


# 客户端路径必须是有效的绝对目录，非法输入不创建会话、不发布事件。
@pytest.mark.parametrize("kind", ["relative", "missing", "file", "nul", "newline", "c1"])
async def test_invalid_workspace_fails_before_persisting(tmp_path: Path, kind: str) -> None:
    file = tmp_path / "file"
    file.write_text("data")
    candidate = {
        "relative": Path("relative"), "missing": tmp_path / "missing", "file": file,
        "nul": Path(str(tmp_path) + "\0bad"),
        "newline": Path(str(tmp_path) + "\nbad"), "c1": Path(str(tmp_path) + "\x85bad"),
    }[kind]
    store = SessionStore(tmp_path / "sessions")
    bus = EventBus()
    events = AsyncMock()
    bus.subscribe(events)
    manager = SessionManager(store, Mock(), bus, workspace=tmp_path)
    with pytest.raises(HandlerError) as exc:
        await manager.create("chat", workspace=candidate)
    assert exc.value.code == INVALID_PARAMS
    assert not store.list_meta()
    assert not manager._sessions
    events.assert_not_called()


# 同一 Core 保存两个客户端项目；重启后按客户端目录恢复，不改变 Core cwd。
async def test_workspace_persists_and_resume_cannot_rebind(tmp_path: Path) -> None:
    projects = [tmp_path / name for name in ("core", "client a 中文", "client b")]
    for project in projects:
        project.mkdir()
    core, first, second = projects
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, Mock(), EventBus(), workspace=core)
    a = await manager.create("chat", workspace=first)
    b = await manager.create("chat", workspace=second)
    legacy_client = await manager.create("chat")
    assert legacy_client.workspace == str(core)
    assert [s.id for s in manager.list_sessions(workspace=first)] == [a.id]
    assert [s.id for s in manager.list_sessions(workspace=second)] == [b.id]
    restarted = SessionManager(store, Mock(), EventBus(), workspace=core)
    restored = await restarted.resume(a.id, workspace=first)
    assert restored.workspace == str(first)
    before = store.read_meta(a.id)
    with pytest.raises(HandlerError) as exc:
        await restarted.resume(a.id, workspace=second)
    assert exc.value.code == SESSION_PROJECT_MISMATCH
    assert store.read_meta(a.id) == before
    assert restored.workspace == str(first)


# 功能：根工具、子 Agent 与嵌套子 Agent 共享会话目录，相对写入不落到 Core
# 设计：经真实 spawn 捕获每层工具表并实际写文件，验证统一内核仍传递原会话目录
async def test_tools_and_nested_agents_use_session_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, project = tmp_path / "core", tmp_path / "project"
    core.mkdir()
    project.mkdir()
    monkeypatch.chdir(core)
    provider = Mock(chat=AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="done")))
    bus = EventBus()
    runner = AgentRunner(KamaConfig(), provider=provider, runs_dir=tmp_path / "runs")
    manager = SessionManager(SessionStore(tmp_path / "sessions"), lambda: runner, bus)
    session = await manager.create("chat", workspace=project)
    runner.bind_registry(manager.agent_registry(session.id))
    built = []
    original = runner._build_registry

    # 捕获每层实际执行创建的工具表
    def capture(*args, **kwargs):
        registry = original(*args, **kwargs)
        built.append(registry)
        return registry

    monkeypatch.setattr(runner, "_build_registry", capture)
    registry = runner._build_registry(
        TaskManager(tmp_path / "tasks"), session=session, provider=provider,
        bus=bus, run_id="root", session_id=session.id,
    )
    for depth in range(3):
        bash = registry.get("bash")
        assert isinstance(bash, BashTool)
        assert bash._cwd == project
        assert bash.path_access.cwd == project
        assert project in bash.path_access.policy.file_system.write_roots
        assert core not in bash.path_access.policy.file_system.write_roots
        writer = registry.get("write_file")
        assert writer is not None
        assert not (await writer.invoke({"path": f"depth-{depth}.txt", "content": "session"})).is_error
        assert (project / f"depth-{depth}.txt").read_text() == "session"
        assert not (core / f"depth-{depth}.txt").exists()
        if depth < 2:
            spawn = registry.get("spawn_agent")
            assert isinstance(spawn, SpawnAgentTool)
            result = await spawn.invoke({"description": "nested", "prompt": "work"})
            assert not result.is_error
            registry = built[-1]
    assert Path.cwd() == core
    await manager.shutdown()


# 项目上下文和 slash skill 都从会话工作区读取，不混入 Core 项目的文件。
async def test_context_and_skill_use_session_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, project = tmp_path / "core", tmp_path / "project"
    for directory, marker in ((core, "CORE_ONLY"), (project, "CLIENT_ONLY")):
        (directory / ".kama" / "skills").mkdir(parents=True)
        (directory / ".kama" / "context.md").write_text(f"{marker}_CONTEXT")
        (directory / ".kama" / "skills" / "workspace_probe.md").write_text(
            f"{marker}_SKILL $ARGUMENTS",
        )
    monkeypatch.chdir(core)
    provider = Mock(chat=AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="done")))
    runner = AgentRunner(KamaConfig(), provider=provider)
    manager = SessionManager(SessionStore(tmp_path / "sessions"), lambda: runner, EventBus())
    session = await manager.create("chat", workspace=project)
    await manager.send_message(session.id, "/workspace_probe hello")
    system = provider.chat.call_args.kwargs["system"]
    assert "CLIENT_ONLY_CONTEXT" in system
    assert "CLIENT_ONLY_SKILL" in system
    assert "CORE_ONLY" not in system
    await manager.send_message(session.id, "ordinary next turn")
    next_system = provider.chat.call_args.kwargs["system"]
    assert "CLIENT_ONLY_CONTEXT" in next_system
    assert "CLIENT_ONLY_SKILL" not in next_system
    assert Path.cwd() == core
    await manager.shutdown()
