from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import BaseModel

from kama_claude.core.bus.events import PermissionRequestedEvent
from kama_claude.core.config import KamaConfig, _apply_toml
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.paths import PathAccessController, path_execution
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.sandbox.linux.bwrap import build_bwrap_args
from kama_claude.core.sandbox.models import FileSystemPolicy, SandboxPolicy
from kama_claude.core.sandbox.policy import configured_policy
from kama_claude.core.subagent.registry import BackgroundTaskRegistry
from kama_claude.core.subagent.tool import SpawnAgentTool
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.builtin import (
    BashTool,
    CreateDirTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry


class Harness:
    # 构造相邻工作区和外部目标，工具策略放行以独立检验路径门禁
    def __init__(self, root: Path) -> None:
        self.work = root / "project"
        self.work.mkdir()
        self.target = root / "中文 target [one]"
        self.access = PathAccessController(self.work)
        self.manager = PermissionManager({name: ToolPolicy(PermissionDecision.ALLOW) for name in (
            "read_file", "write_file", "create_dir", "list_dir", "bash",
        )}, timeout_s=0.05)
        self.bus = EventBus()
        self.requests: list[PermissionRequestedEvent] = []
        self.decision: str | None = "allow_once"
        self.bus.subscribe(self.respond)

    # 在真正执行前检查审批数据，再通过精确请求标识作出决定
    async def respond(self, event: BaseModel) -> None:
        if isinstance(event, PermissionRequestedEvent):
            self.requests.append(event)
            if self.decision:
                assert self.manager.respond(event.tool_use_id, self.decision, event.request_id)

    # 使用真实调用编排与工具，保留会话维度并允许模拟重复工具 ID
    async def call(
        self, tool: BaseTool, params: dict[str, Any], *, sid: str = "s1", uid: str = "tool",
    ) -> ToolResult:
        registry = ToolRegistry()
        registry.register(tool)
        return await invoke_tool(
            registry, ToolCallBlock(id=uid, name=tool.name, input=params),
            self.bus, "run", permission_manager=self.manager, session_id=sid,
        )


# 功能：目录创建请求只授权精确目标，会话权限不能覆盖父目录或前缀相邻目录
# 设计：经过真实审批和宿主创建，比较审批数据、缓存、沙箱策略与磁盘结果
async def test_create_exact_directory_and_session_scope(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.decision = "allow_session"
    result = await h.call(CreateDirTool(h.access), {"path": str(h.target)})
    assert not result.is_error, result.content
    request = h.requests[0]
    assert [p.path for p in request.paths] == [str(h.target)]
    assert request.paths[0].kind == "directory"
    assert request.paths[0].operation == "create_dir"
    assert "always_allow" not in request.allowed_decisions
    assert h.target.is_dir()
    grants = h.manager.session_grants("s1", str(h.work))
    with path_execution(h.access, grants):
        policy = h.access.sandbox_policy()
        assert set(policy.file_system.write_roots) == {h.work, h.target}
    result = await h.call(WriteFileTool(h.access), {
        "path": str(h.target / "nested" / "ok.txt"), "content": "ok",
    })
    assert not result.is_error, result.content
    assert len(h.requests) == 1
    h.decision = "deny_once"
    for target in (tmp_path / "other.txt", tmp_path / (h.target.name + "-sibling") / "x"):
        result = await h.call(WriteFileTool(h.access), {"path": str(target), "content": "bad"})
        assert result.is_error
        assert not target.exists()
    assert len(h.requests) == 3


# 功能：单次创建授权不被下一次操作或其他会话复用
# 设计：创建成功后拒绝下一次文件写入，并检查目标及授权缓存
async def test_once_and_session_isolation(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert not (await h.call(CreateDirTool(h.access), {"path": str(h.target)})).is_error
    assert not h.manager.session_grants("s1", str(h.work))
    h.decision = "deny_once"
    assert (await h.call(WriteFileTool(h.access), {
        "path": str(h.target / "denied"), "content": "x",
    })).is_error
    h.decision = "allow_session"
    assert not (await h.call(CreateDirTool(h.access), {"path": str(h.target)})).is_error
    h.decision = "deny_once"
    assert (await h.call(WriteFileTool(h.access), {
        "path": str(h.target / "other-session"), "content": "x",
    }, sid="s2")).is_error
    h.manager.cancel_session("s1")
    assert not h.manager.session_grants("s1", str(h.work))
    assert (await h.call(CreateDirTool(h.access), {"path": str(h.target)})).is_error


# 功能：拒绝或超时不产生任何创建副作用，过期响应不能追加授权
# 设计：等待真实权限超时后重放同一个请求，检查磁盘和缓存都未改变
@pytest.mark.parametrize("decision", ["deny_once", None])
async def test_denial_and_timeout_leave_no_files(tmp_path: Path, decision: str | None) -> None:
    h = Harness(tmp_path)
    h.decision = decision
    result = await h.call(CreateDirTool(h.access), {"path": str(h.target)})
    assert result.error_type == "permission_denied"
    assert not h.target.exists()
    request = h.requests[0]
    assert not h.manager.respond("tool", "allow_session", request.request_id)
    assert not h.manager._pending
    assert not h.manager.session_grants("s1", str(h.work))


# 功能：精确文件批准只写该文件，不能修改同级文件或隐式创建外部父目录
# 设计：真实写入后尝试同级目标与缺失父目录，并检查没有部分目录副作用
async def test_exact_file_and_missing_parent(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.decision = "allow_session"
    result = await h.call(WriteFileTool(h.access), {"path": str(h.target), "content": "hello"})
    assert not result.is_error, result.content
    assert h.target.read_text() == "hello"
    assert h.requests[0].paths[0].kind == "file"
    h.decision = "deny_once"
    assert (await h.call(WriteFileTool(h.access), {
        "path": str(tmp_path / "sibling"), "content": "bad",
    })).is_error
    h.decision = "allow_once"
    result = await h.call(WriteFileTool(h.access), {
        "path": str(tmp_path / "missing" / "file.txt"), "content": "bad",
    })
    assert result.error_type == "path_not_ready"
    assert "create_dir" in result.content
    assert not (tmp_path / "missing").exists()


# 功能：Shell 声明不是授权，缺失目标不请求父目录且不启动命令
# 设计：不用模拟沙箱，确保预检在任何后端调用之前就返回确定性错误
async def test_shell_declaration_requires_approval_and_existing_target(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    bash = BashTool(access=h.access)
    result = await h.call(bash, {"command": "exit 99", "required_paths": [
        {"path": str(h.target), "access": "write"},
    ]})
    assert result.error_type == "path_not_ready"
    assert not h.requests
    h.target.mkdir()
    result = await bash.invoke({"command": "exit 99", "required_paths": [
        {"path": str(h.target), "access": "write"},
    ]})
    assert result.error_type == "permission_denied"


# 功能：读取会话授权不能升级为写权限，规范化后的外部路径仍需批准
# 设计：先批准含 .. 的读操作，再拒绝同一目标的写操作并检查原内容
async def test_read_grant_cannot_write(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.target.write_text("secret", encoding="utf-8")
    h.decision = "allow_session"
    result = await h.call(ReadFileTool(h.access), {"path": str(Path("..") / h.target.name)})
    assert result.content == "secret"
    assert h.requests[0].paths[0].path == str(h.target)
    h.decision = "deny_once"
    result = await h.call(WriteFileTool(h.access), {"path": str(h.target), "content": "bad"})
    assert result.error_type == "permission_denied"
    assert h.target.read_text() == "secret"


# 功能：禁止路径和受保护目录不可通过审批覆盖，目录列表不泄漏禁止项
# 设计：同一策略用于写、读和列目录，验证没有发起可绕过禁止项的审批
async def test_hard_denials_and_listing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    secret = h.work / "secret"
    secret.write_text("secret")
    (h.work / "ok").write_text("ok")
    access = PathAccessController(h.work, SandboxPolicy(FileSystemPolicy(
        read_roots=(h.work,), write_roots=(h.work,), deny_paths=(secret,),
    )))
    assert (await h.call(ReadFileTool(access), {"path": str(secret)})).is_error
    assert (await h.call(WriteFileTool(access), {"path": str(h.work / ".git" / "x"),
                                               "content": "bad"})).is_error
    result = await h.call(ListDirTool(access), {"path": str(h.work)})
    assert "secret" not in result.content and "ok" in result.content
    assert not h.requests


# 功能：旧客户端、永久工具批准、伪造请求 ID 和错误会话均不能批准路径
# 设计：在真实 pending 期间发送无效决策，最后正确响应只保存会话路径权限
async def test_response_binding_and_no_persistence(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    policy_file = tmp_path / "policy.toml"
    h.manager._policy_file = policy_file
    h.manager._persistent_always["create_dir"] = "allow"
    h.decision = None
    task = asyncio.create_task(h.call(CreateDirTool(h.access), {"path": str(h.target)}))
    await asyncio.sleep(0)
    request = h.requests[0]
    assert not h.target.exists()
    assert not h.manager.respond("tool", "allow_once")
    assert not h.manager.respond("tool", "always_allow", request.request_id)
    assert not h.manager.respond("tool", "allow_once", "forged")
    assert not h.manager.respond("tool", "allow_once", request.request_id, session_id="s2")
    assert h.manager.respond("tool", "allow_session", request.request_id, session_id="s1")
    assert not (await task).is_error
    assert not policy_file.exists()


# 功能：并发同名工具 ID 使用独立请求，取消和断连不产生授权
# 设计：两个会话挂起相同工具 ID，取消其中一个而另一个单次批准
async def test_concurrency_cancellation_and_disconnect(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.decision = None
    h.manager._timeout_s = 0
    first = asyncio.create_task(h.call(CreateDirTool(h.access), {"path": str(h.target)}))
    second_target = tmp_path / "second"
    second = asyncio.create_task(h.call(CreateDirTool(h.access), {"path": str(second_target)}, sid="s2"))
    await asyncio.sleep(0)
    assert len(h.requests) == 2
    assert h.requests[0].request_id != h.requests[1].request_id
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not h.manager.respond("tool", "allow_session", h.requests[0].request_id)
    assert h.manager.respond("tool", "allow_once", h.requests[1].request_id)
    assert not (await second).is_error
    assert not h.target.exists() and second_target.is_dir()
    third = asyncio.create_task(h.call(CreateDirTool(h.access), {"path": str(h.target)}))
    await asyncio.sleep(0)
    h.manager.cancel_session("s1")
    assert (await third).is_error
    assert not h.manager._pending


# 功能：审批期间符号链接替换不能将已批准操作重定向到另一个目录
# 设计：用真实链接重定向外部父目录，等待后的复查必须阻止实际文件创建
async def test_symlink_swap_after_request(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    original = tmp_path / "original"
    original.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link-probe"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit symlink creation")
    link.unlink()
    h.decision = None
    target = original / "file.txt"
    task = asyncio.create_task(h.call(WriteFileTool(h.access), {"path": str(target), "content": "bad"}))
    await asyncio.sleep(0)
    original.rmdir()
    original.symlink_to(outside, target_is_directory=True)
    try:
        assert h.manager.respond("tool", "allow_once", h.requests[0].request_id)
        result = await task
        assert result.error_type == "sandbox_policy_error"
        assert not (outside / "file.txt").exists()
    finally:
        original.unlink()


# 功能：硬链接写入不会在审批后修改另一个路径的内容
# 设计：以真实硬链接连接工作区与外部文件，写入前必须识别共享 inode
async def test_hardlink_write_is_refused(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.target.write_text("original")
    link = h.work / "linked"
    os.link(h.target, link)
    result = await h.call(WriteFileTool(h.access), {"path": str(link), "content": "bad"})
    assert result.error_type == "permission_denied"
    assert h.target.read_text() == "original"


# 功能：通用配置映射到现有模型并与 Windows 配置合并，相对路径以工作区解析
# 设计：检查实际控制器策略及禁止项，排除新配置只被解析却未生效
def test_filesystem_config_uses_existing_policy(tmp_path: Path) -> None:
    work = tmp_path / "project"
    work.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    secret = extra / "secret"
    secret.write_text("secret")
    config = KamaConfig()
    _apply_toml(config, {"sandbox": {"filesystem": {
        "readonly_paths": ["../extra"], "denied_paths": [str(secret)],
    }}})
    access = PathAccessController(work, configured_policy(
        work, config.sandbox.filesystem, config.sandbox.windows,
    ))
    assert extra in access.policy.file_system.read_roots
    assert secret in access.policy.file_system.deny_paths
    assert Path(work.anchor) not in access.policy.file_system.read_roots


# 功能：同会话子 Agent 能复用精确目录权限，注册的新建目录工具沿用同一控制器
# 设计：通过实际 spawn 捕获共用内核的工具表，确认没有额外路径申请和父目录授权
async def test_child_registry_reuses_session_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kama_claude.core.llm.types import LlmResponse
    from kama_claude.core.runner import AgentRunner

    built = []
    original = AgentRunner._build_registry

    # 捕获实际子执行创建的工具表
    def capture(self, *args, **kwargs):
        registry = original(self, *args, **kwargs)
        built.append(registry)
        return registry

    monkeypatch.setattr(AgentRunner, "_build_registry", capture)
    h = Harness(tmp_path)
    h.decision = "allow_session"
    assert not (await h.call(CreateDirTool(h.access), {"path": str(h.target)})).is_error
    provider = Mock(chat=AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="done")))
    agents = BackgroundTaskRegistry()
    spawn = SpawnAgentTool(provider, h.bus, "parent", h.manager, 3, agents,
                           tmp_path / "runs", "s1", filesystem_access=h.access)
    assert not (await spawn.invoke({"description": "worker", "prompt": "work"})).is_error
    registry = built[-1]
    assert registry.get("create_dir").path_access is h.access
    result = await h.call(registry.get("write_file"), {
        "path": str(h.target / "from-child"), "content": "ok",
    })
    assert not result.is_error, result.content
    assert len(h.requests) == 1
    nested = registry.get("spawn_agent")
    assert not (await nested.invoke({"description": "nested", "prompt": "work"})).is_error
    assert built[-1].get("write_file").path_access is h.access
    await agents.shutdown()


# 功能：Windows junction 在审批等待期间替换父路径时不能重定向写入
# 设计：使用不要求符号链接权限的真实目录联接，审批后复查必须在写入前失败
@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction test")
async def test_windows_junction_swap_after_approval(tmp_path: Path) -> None:
    import _winapi

    h = Harness(tmp_path)
    h.decision = None
    original = tmp_path / "original"
    original.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = original / "file.txt"
    task = asyncio.create_task(h.call(WriteFileTool(h.access), {"path": str(target), "content": "bad"}))
    await asyncio.sleep(0)
    original.rmdir()
    _winapi.CreateJunction(str(outside), str(original))
    try:
        assert h.manager.respond("tool", "allow_once", h.requests[0].request_id)
        result = await task
        assert result.error_type == "sandbox_policy_error"
        assert not (outside / "file.txt").exists()
    finally:
        original.rmdir()


# 功能：类型冲突和父目录缺失时不创建任何意料外的目标
# 设计：对文件请求建目录、对目录请求写文件，并请求缺失父目录下的目录
async def test_create_type_conflicts_and_missing_parent(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.target.write_text("keep")
    assert (await h.call(CreateDirTool(h.access), {"path": str(h.target)})).is_error
    assert (await h.call(WriteFileTool(h.access), {"path": str(h.work), "content": "bad"})).is_error
    assert not h.requests
    result = await h.call(CreateDirTool(h.access), {"path": str(tmp_path / "missing" / "child")})
    assert result.error_type == "path_not_ready"
    assert not (tmp_path / "missing").exists()
    assert h.target.read_text() == "keep"


# 功能：Linux 的私有临时目录不会覆盖已批准的只读路径挂载
# 设计：检查挂载顺序，确保创建私有 /tmp 后才绑定精确读取根，覆盖窄读取策略的回归
def test_linux_private_tmp_precedes_read_grants(tmp_path: Path) -> None:
    policy = SandboxPolicy(FileSystemPolicy(read_roots=(tmp_path,), include_platform_defaults=False))
    args = build_bwrap_args(policy, tmp_path, str)
    assert args.index("/tmp") < args.index("--ro-bind")
