from __future__ import annotations

import asyncio
import importlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.sandbox import (
    FileSystemPolicy,
    NetworkPolicy,
    SandboxCommand,
    SandboxExecRequest,
    SandboxExecResult,
    SandboxManager,
    SandboxPolicy,
    SandboxPolicyError,
    SandboxSetupError,
    SandboxType,
    SandboxUnavailableError,
    UnsupportedSandboxError,
    with_approved_permissions,
    workspace_policy,
)
from kama_claude.core.sandbox.denial import is_suspected_denial
from kama_claude.core.sandbox.linux.bwrap import build_bwrap_args
from kama_claude.core.sandbox.linux.command import build_command
from kama_claude.core.sandbox.macos.seatbelt import build_profile
from kama_claude.core.sandbox.macos.seatbelt_scratch import process_scratch
from kama_claude.core.sandbox.policy import clean_environment, normalize_policy
from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry

spawn_module = importlib.import_module("kama_claude.core.sandbox.spawn")


# 功能：验证默认策略限制工作区外写入，并保留存在的元数据保护目录
# 设计：建立真实工作区与 .git，检查规范化结果及缺失目录不会在宿主被创建
def test_workspace_defaults(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    policy = normalize_policy(workspace_policy(tmp_path), tmp_path)
    assert policy.network == NetworkPolicy.DENY
    assert policy.file_system.write_roots == (tmp_path.resolve(),)
    assert tmp_path.resolve() / ".git" in policy.file_system.protected_paths
    assert not (tmp_path / ".agents").exists()


# 功能：验证审批新增写权限不能删除原有拒绝路径
# 设计：同一目录同时获得额外写授权与原始拒绝项，要求两者都传给后端
def test_merge_preserves_denials(tmp_path: Path) -> None:
    policy = SandboxPolicy(FileSystemPolicy(deny_paths=(tmp_path,)))
    merged = with_approved_permissions(policy, write_roots=(tmp_path,))
    assert merged.file_system.deny_paths == (tmp_path,)
    assert merged.file_system.write_roots == (tmp_path,)


# 功能：验证缺失授权路径报错，而缺失限制路径收紧到现有祖先
# 设计：使用同一个不存在路径分别作为授权与拒绝项，避免挂载过程隐式创建宿主路径
def test_missing_paths_fail_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "secret"
    with pytest.raises(SandboxPolicyError, match="does not exist"):
        normalize_policy(SandboxPolicy(FileSystemPolicy(write_roots=(missing,))), tmp_path)
    policy = normalize_policy(SandboxPolicy(FileSystemPolicy(deny_paths=(missing,))), tmp_path)
    assert policy.file_system.deny_paths == (tmp_path.resolve(),)
    assert not missing.parent.exists()


# 功能：验证可替换符号链接不能作为沙盒授权根
# 设计：伪造单个组件的符号链接属性，跨平台检查策略验证而不依赖 Windows 链接权限
def test_symlink_grant_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == tmp_path or original(p))
    with pytest.raises(SandboxPolicyError, match="symlink"):
        normalize_policy(SandboxPolicy(FileSystemPolicy(write_roots=(tmp_path,))), tmp_path)


# 功能：验证环境清理删除隔离前执行代码的启动钩子
# 设计：保留普通环境配置，同时覆盖 Python、动态链接器、shell 与字符转换钩子
def test_environment_removes_pre_sandbox_hooks() -> None:
    env = {key: "value" for key in (
        "PATH", "HOME", "HTTPS_PROXY", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH", "BASH_ENV", "ENV", "BASH_FUNC_attack%%", "GCONV_PATH",
    )}
    assert clean_environment(env) == {key: "value" for key in ("PATH", "HOME", "HTTPS_PROXY")}


# 功能：验证未支持的平台明确失败且不调用任何后端
# 设计：在真实 prepare 边界检查错误类型，排除静默无沙盒执行
def test_unsupported_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "freebsd")
    with pytest.raises(UnsupportedSandboxError):
        SandboxManager().prepare(SandboxCommand(("echo", "hi"), tmp_path), workspace_policy(tmp_path))


# 功能：验证 Linux 后端缺失不会退回原始命令
# 设计：只替换依赖探测，保留管理器的策略处理与错误传播
def test_missing_bwrap_is_not_bypassed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "kama_claude.core.sandbox.linux.command.find_bwrap",
        Mock(side_effect=SandboxUnavailableError("missing bwrap")),
    )
    with pytest.raises(SandboxUnavailableError):
        SandboxManager().prepare(SandboxCommand(("echo",), tmp_path), workspace_policy(tmp_path))


# 功能：验证启动协议保持命令边界且使用隔离的 Python 启动模式
# 设计：在命令参数中放入空格、引号与 shell 元字符，解析 JSON 后逐项比较
def test_linux_command_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kama_claude.core.sandbox.linux.command.find_bwrap", lambda: Path("/usr/bin/bwrap"),
    )
    command = SandboxCommand(("/bin/sh", "-c", "printf '%s' 'a b;$(x)'"), tmp_path)
    argv = build_command(command, workspace_policy(tmp_path))
    assert argv[1:3] == ("-I", "-S")
    assert argv[-2] == "--outer"
    assert json.loads(argv[-1])["argv"] == list(command.argv)


# 功能：验证挂载优先级不会让可写父目录覆盖保护项和禁止项
# 设计：构造重叠目录并检查执行顺序，同时确认网络与 PID 隔离存在
def test_linux_mount_order(tmp_path: Path) -> None:
    protected = tmp_path / ".git"
    denied = tmp_path / "secret"
    protected.mkdir()
    denied.mkdir()
    policy = SandboxPolicy(FileSystemPolicy(
        write_roots=(tmp_path,), protected_paths=(protected,), deny_paths=(denied,),
    ))
    args = build_bwrap_args(policy, tmp_path, lambda p: f"fd:{p}")
    assert "--unshare-net" in args and "--unshare-pid" in args
    assert "--disable-userns" in args
    assert args.index("--bind") < args.index(f"fd:{protected}") < args.index("0000")
    allowed = build_bwrap_args(replace(policy, network=NetworkPolicy.ALLOW), tmp_path, str)
    assert "--unshare-net" not in allowed
    assert "--unshare-pid" in allowed


# 功能：验证限制读取模式从空根开始，而不会暴露整个宿主文件系统
# 设计：关闭系统默认读取，给出单一真实目录，并检查不存在全盘只读挂载
def test_linux_restricted_read_root(tmp_path: Path) -> None:
    policy = SandboxPolicy(FileSystemPolicy(read_roots=(tmp_path,), include_platform_defaults=False))
    args = build_bwrap_args(policy, tmp_path, str)
    assert ["--tmpfs", "/"] == args[args.index("--tmpfs"):args.index("--tmpfs") + 2]
    assert not any(args[i:i + 3] == ["--ro-bind", "/", "/"] for i in range(len(args)))
    assert args.count(str(tmp_path.resolve())) >= 2


# 功能：验证 SBPL 动态路径全部参数化，恶意路径文本不会进入规则
# 设计：用引号与括号构造未解析路径，仅测试生成器的参数边界
def test_seatbelt_parameterizes_paths() -> None:
    path = Path('/workspace/a") (allow default) ;')
    profile, parameters = build_profile(SandboxPolicy(FileSystemPolicy(write_roots=(path,))))
    assert str(path) in parameters.values()
    assert str(path) not in profile
    assert "(deny default)" in profile
    assert "(deny network*)" in profile


# 功能：验证读取限制会关闭偏好服务，且网络规则只按显式策略放行
# 设计：对比默认全读、限制读取和网络允许三种独立策略
def test_seatbelt_network_and_preferences(tmp_path: Path) -> None:
    full, _ = build_profile(SandboxPolicy())
    assert "user-preference-read" in full
    restricted, _ = build_profile(SandboxPolicy(FileSystemPolicy(read_roots=(tmp_path,))))
    assert "user-preference-read" not in restricted
    network, _ = build_profile(SandboxPolicy(network=NetworkPolicy.ALLOW))
    assert '(remote ip "*:*")' in network
    assert "(deny network*)" not in network


# 功能：验证保护目录与祖先重命名同时受到 Seatbelt 限制
# 设计：给可写目录添加保护子目录，检查排除过滤器与祖先 unlink 限制
def test_seatbelt_protected_ancestor(tmp_path: Path) -> None:
    protected = tmp_path / ".git"
    protected.mkdir()
    profile, parameters = build_profile(SandboxPolicy(FileSystemPolicy(
        write_roots=(tmp_path,), protected_paths=(protected,),
    )))
    assert "require-not" in profile
    assert "(deny file-write-unlink" in profile
    assert str(tmp_path) in parameters.values()
    assert str(protected) in parameters.values()


# 功能：验证私有临时目录在失败退出后清理且不扩大整个系统临时目录权限
# 设计：上下文内抛出异常，再验证唯一授权目录已删除
def test_macos_scratch_lifecycle() -> None:
    root: Path | None = None
    with pytest.raises(RuntimeError):
        with process_scratch(SandboxPolicy(), {}) as (policy, env):
            root = Path(env["TMPDIR"])
            assert root.is_dir()
            assert policy.file_system.write_roots == (root,)
            raise RuntimeError("cancel execution")
    assert root is not None and not root.exists()


# 功能：验证只有失败命令的权限诊断才被标记为疑似沙盒拒绝
# 设计：包括正常退出但打印权限文本的情况，防止内容匹配覆盖成功状态
@pytest.mark.parametrize(("code", "text", "expected"), [
    (0, "permission denied", False), (1, "Permission denied", True),
    (1, "Read-only file system", True), (2, "syntax error", False),
])
def test_denial_detection(code: int, text: str, expected: bool) -> None:
    assert is_suspected_denial(code, text) is expected


# 功能：验证大输出被持续排空，但内存保留量不超过字节限额
# 设计：使用真实 asyncio 流分块读取，检查 UTF-8 字节限额与截断信号
async def test_output_collection_bounded() -> None:
    stream = asyncio.StreamReader()
    stream.feed_data("你好".encode() * 10000)
    stream.feed_eof()
    output = bytearray()
    truncated = await spawn_module._read_output(stream, output, 101)
    assert truncated and len(output) == 101
    assert await stream.read() == b""


# 功能：验证 Bash 返回值格式与执行器结果一致
# 设计：只替换执行器与准备边界，覆盖成功、命令失败、超时、拒绝和截断
@pytest.mark.parametrize(("execution", "error", "fragment"), [
    (SandboxExecResult(0, "hello"), None, "hello"),
    (SandboxExecResult(2, "bad"), "command_failed", "[exit 2]"),
    (SandboxExecResult(-9, "partial", timed_out=True), "timeout", "partial"),
    (SandboxExecResult(1, "denied", suspected_denial=True), "sandbox_denied", "denied"),
    (SandboxExecResult(0, "hello", truncated=True), None, "[truncated]"),
])
async def test_bash_result_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    execution: SandboxExecResult, error: str | None, fragment: str,
) -> None:
    request = SandboxExecRequest(("wrapped",), tmp_path, {}, SandboxType.LINUX)
    manager = Mock(spec=SandboxManager)
    manager.prepare.return_value = request
    executor = AsyncMock(return_value=execution)
    monkeypatch.setattr("kama_claude.core.tools.builtin.bash.spawn", executor)
    result = await BashTool(cwd=tmp_path, manager=manager).invoke({"command": "echo hello"})
    assert result.error_type == error
    assert fragment in result.content
    executor.assert_awaited_once_with(request)


# 功能：验证沙盒初始化错误只产生一次工具失败，不进入通用重试
# 设计：通过真实 invoke_tool 调用 Bash，并计数 prepare 调用次数
async def test_sandbox_failure_not_retried(tmp_path: Path) -> None:
    manager = Mock(spec=SandboxManager)
    manager.prepare.side_effect = SandboxSetupError("failed to install policy")
    registry = ToolRegistry()
    registry.register(BashTool(cwd=tmp_path, manager=manager))
    result = await invoke_tool(
        registry, ToolCallBlock(id="sandbox-test", name="bash", input={"command": "echo hi"}),
        EventBus(), "run-test",
    )
    assert result.error_type == "sandbox_setup_error"
    assert manager.prepare.call_count == 1


# 功能：验证取消操作会传递给调用方，不变成可重试的工具错误
# 设计：执行器抛出 CancelledError，检查 Bash 不捕获或吞掉取消信号
async def test_bash_propagates_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = Mock(spec=SandboxManager)
    monkeypatch.setattr(
        "kama_claude.core.tools.builtin.bash.spawn", AsyncMock(side_effect=asyncio.CancelledError),
    )
    with pytest.raises(asyncio.CancelledError):
        await BashTool(cwd=tmp_path, manager=manager).invoke({"command": "sleep 10"})


# 功能：验证初始化与子进程异常均经过不可重试的沙盒错误边界
# 设计：模拟系统启动失败，不依赖测试宿主实际存在沙盒二进制
async def test_spawn_oserror_classified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    creator = AsyncMock(side_effect=OSError("cannot exec backend"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", creator)
    with pytest.raises(SandboxSetupError, match="cannot exec"):
        await spawn_module.spawn(SandboxExecRequest(("backend",), tmp_path, {}, SandboxType.LINUX))


# 功能：验证超时和取消都会触发进程组清理
# 设计：阻塞模拟进程等待，观察执行器调用清理并保留取消语义
@pytest.mark.parametrize("cancel", [False, True])
async def test_spawn_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    proc = Mock()
    proc.stdout = asyncio.StreamReader()
    proc.returncode = -9
    started = asyncio.Event()

    # 模拟持续运行的子进程，直到外层超时或取消
    async def wait_forever() -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(spawn_module, "_wait_exit", lambda process: wait_forever())
    stopper = AsyncMock()
    monkeypatch.setattr(spawn_module, "_stop", stopper)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    request = SandboxExecRequest(("backend",), tmp_path, {}, SandboxType.LINUX, timeout=0.03)
    task = asyncio.create_task(spawn_module.spawn(request))
    await started.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task).timed_out
    stopper.assert_awaited_once_with(proc)


# 功能：验证 seccomp 安装失败时不会执行目标命令或接受空过滤器
# 设计：用 ctypes 接口替身返回内核加载错误，并确认过滤器被释放
def test_seccomp_load_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from kama_claude.core.sandbox.linux import seccomp

    library = Mock()
    library.seccomp_init.return_value = 123
    library.seccomp_syscall_resolve_name.return_value = 42
    library.seccomp_rule_add_array.return_value = 0
    library.seccomp_load.return_value = -1
    libc = Mock()
    libc.prctl.return_value = 0
    monkeypatch.setattr(seccomp, "load_seccomp", lambda: library)
    monkeypatch.setattr(seccomp.ctypes, "CDLL", lambda *args, **kwargs: libc)
    with pytest.raises(SandboxSetupError, match="install seccomp"):
        seccomp.apply_seccomp(NetworkPolicy.DENY)
    library.seccomp_release.assert_called_once_with(123)


# 功能：验证 seccomp 的 socket 规则符合单参数单比较限制且不放行宿主 Unix socket
# 设计：记录生成的过滤器规则，避免仅靠字符串测试漏掉 libseccomp 拒绝规则的问题
def test_seccomp_socket_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    from kama_claude.core.sandbox.linux import seccomp

    library = Mock()
    library.seccomp_init.return_value = 123
    library.seccomp_syscall_resolve_name.return_value = 42
    library.seccomp_rule_add_array.return_value = 0
    library.seccomp_load.return_value = 0
    libc = Mock()
    libc.prctl.return_value = 0
    monkeypatch.setattr(seccomp, "load_seccomp", lambda: library)
    monkeypatch.setattr(seccomp.ctypes, "CDLL", lambda *args, **kwargs: libc)
    seccomp.apply_seccomp(NetworkPolicy.ALLOW)
    calls = library.seccomp_rule_add_array.call_args_list
    assert all(call.args[3] <= 1 for call in calls)
    assert any(call.args[3] == 1 and call.args[4].contents.datum_a == 40 for call in calls)


# 功能：验证 Linux 辅助进程在安装过滤器和运行命令前关闭宿主挂载描述符
# 设计：在 seccomp 边界中断执行，并记录验证与关闭顺序，防止宿主目录句柄泄漏
def test_linux_closes_host_fds_before_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from kama_claude.core.sandbox.linux import runner

    events: list[str] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(runner, "verify_mounts", lambda identities: events.append("verified"))
    monkeypatch.setattr(runner.os, "close", lambda fd: events.append(f"closed:{fd}"))
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: "CapEff:\t0\nCapPrm:\t0\n")
    monkeypatch.setattr(runner, "apply_seccomp", Mock(side_effect=SandboxSetupError("stop test")))
    with pytest.raises(SandboxSetupError, match="stop test"):
        runner._inner({"identities": [], "mount_fds": [7, 8], "network": "deny"})
    assert events == ["verified", "closed:7", "closed:8"]


# 功能：验证真实管理器会把辅助程序及解释器运行时加入不可写范围
# 设计：只替换最终参数生成，检查传给后端的策略，覆盖 editable 安装处于工作区内的情况
def test_manager_protects_own_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    backend = Mock(return_value=("wrapped",))
    monkeypatch.setattr("kama_claude.core.sandbox.linux.command.build_command", backend)
    request = SandboxManager().prepare(
        SandboxCommand(("/bin/sh", "-c", "echo ok"), tmp_path, {"LD_PRELOAD": "bad"}),
        workspace_policy(tmp_path),
    )
    policy = backend.call_args.args[1]
    assert Path(sys.prefix).resolve() in policy.file_system.protected_paths
    assert Path(sys.executable).resolve() in policy.file_system.protected_paths
    assert "LD_PRELOAD" not in request.env
