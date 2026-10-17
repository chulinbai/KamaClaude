from __future__ import annotations

import asyncio
import shlex
import socket
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from kama_claude.core.sandbox import (
    FileSystemPolicy,
    NetworkPolicy,
    SandboxPolicy,
    workspace_policy,
)
from kama_claude.core.tools.builtin.bash import BashTool

pytestmark = [pytest.mark.sandbox_native, pytest.mark.usefixtures("native_sandbox")]


# 功能：验证真实后端允许工作区写入并阻止同级目录的写入
# 设计：在临时目录中设置内外两个目标，检查返回值与宿主文件内容
async def test_native_workspace_boundary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged")
    tool = BashTool(cwd=workspace)
    success = await tool.invoke({"command": "printf hello > allowed.txt; cat allowed.txt"})
    assert not success.is_error, success.content
    assert (workspace / "allowed.txt").read_text() == "hello"
    failure = await tool.invoke({"command": f"printf overwritten > {shlex.quote(str(outside))}"})
    assert failure.is_error
    assert outside.read_text() == "unchanged"


# 功能：验证受保护元数据即使位于可写工作区内部也不可修改
# 设计：创建真实 .git/config，再尝试覆盖和整体移动保护目录
async def test_native_protected_metadata(tmp_path: Path) -> None:
    metadata = tmp_path / ".git"
    metadata.mkdir()
    (metadata / "config").write_text("original")
    tool = BashTool(cwd=tmp_path)
    result = await tool.invoke({"command": "printf changed > .git/config"})
    assert result.is_error
    assert (metadata / "config").read_text() == "original"
    result = await tool.invoke({"command": "mv .git moved-git"})
    assert result.is_error
    assert metadata.is_dir()


# 功能：验证拒绝读取的文件不会通过宽泛全盘读取授权暴露
# 设计：将秘密文件设为 deny，检查内容不出现在任何命令输出中
async def test_native_deny_read(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("sandbox-private-value")
    policy = workspace_policy(tmp_path)
    policy = replace(policy, file_system=replace(policy.file_system, deny_paths=(secret,)))
    result = await BashTool(cwd=tmp_path, policy=policy).invoke({"command": "cat secret"})
    assert "sandbox-private-value" not in result.content
    assert secret.read_text() == "sandbox-private-value"


# 功能：验证符号链接不能把工作区写入授权扩展到外部目录
# 设计：在工作区建立指向相邻目录的链接，再从链接路径尝试写入
async def test_native_symlink_does_not_expand_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    result = await BashTool(cwd=workspace).invoke({"command": "echo escaped > escape/result"})
    assert result.is_error
    assert not (outside / "result").exists()


# 功能：验证网络开关实际控制 TCP 连接
# 设计：只连接测试进程自身的本地监听端口，不访问互联网或第三方服务
@pytest.mark.parametrize("network", [NetworkPolicy.DENY, NetworkPolicy.ALLOW])
async def test_native_network(tmp_path: Path, network: NetworkPolicy) -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        code = f"import socket; socket.create_connection(('127.0.0.1', {port}), 1).close()"
        command = f"{shlex.quote(sys.executable)} -I -c {shlex.quote(code)}"
        policy = replace(workspace_policy(tmp_path), network=network)
        result = await BashTool(cwd=tmp_path, policy=policy).invoke({"command": command})
        assert result.is_error == (network == NetworkPolicy.DENY), result.content


# 功能：验证限制读取策略只暴露授权目录与必需运行时
# 设计：把目标秘密放在工作区旁，确认 shell 可启动而秘密无法读取
async def test_native_restricted_reads(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("restricted-read-secret")
    policy = SandboxPolicy(FileSystemPolicy(read_roots=(workspace,), write_roots=(workspace,)))
    tool = BashTool(cwd=workspace, policy=policy)
    success = await tool.invoke({"command": "echo ready"})
    assert not success.is_error, success.content
    result = await tool.invoke({"command": f"cat {shlex.quote(str(secret))}"})
    assert result.is_error
    assert "restricted-read-secret" not in result.content


# 功能：验证命令超时后普通子进程不能继续写入工作区
# 设计：后台子进程延迟写入标记，超时后等待其原计划完成时间并确认标记未生成
async def test_native_timeout_cleans_children(tmp_path: Path) -> None:
    result = await BashTool(cwd=tmp_path).invoke({
        "command": "(sleep 2; echo leaked > timeout-leak) & wait", "timeout": 1,
    })
    assert result.error_type == "timeout", result.content
    await asyncio.sleep(1.3)
    assert not (tmp_path / "timeout-leak").exists()


# 功能：验证外层任务取消同样会清理命令的普通子进程
# 设计：先等待目标写出就绪标记再取消，排除尚未启动导致的假通过
async def test_native_cancellation_cleans_children(tmp_path: Path) -> None:
    task = asyncio.create_task(BashTool(cwd=tmp_path).invoke({
        "command": "echo ready > started; (sleep 2; echo leaked > cancel-leak) & wait",
    }))
    try:
        async with asyncio.timeout(10):
            while not (tmp_path / "started").exists():
                if task.done():
                    pytest.fail(f"Sandbox exited before ready: {task.result()}")
                await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await asyncio.sleep(2.2)
    assert not (tmp_path / "cancel-leak").exists()
