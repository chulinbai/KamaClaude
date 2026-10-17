from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from ..denial import SETUP_ERROR_PREFIX
from ..errors import SandboxError, SandboxSetupError
from ..models import FileSystemPolicy, NetworkPolicy, SandboxPolicy
from .bwrap import build_bwrap_args
from .fd_mount import MountHandles, verify_mounts
from .seccomp import apply_seccomp, load_seccomp


# 在新 PID 命名空间中验证能力清零，然后安装不可逆的系统调用限制
def _inner(payload: dict[str, Any]) -> int:
    if sys.platform != "linux":
        raise SandboxSetupError("Linux helper cannot run on this platform")
    verify_mounts(payload["identities"])
    # 宿主挂载源描述符不能泄漏给命令，否则 openat 可绕过新的挂载视图
    for fd in payload["mount_fds"]:
        os.close(fd)
    status = Path("/proc/self/status").read_text()
    for line in status.splitlines():
        if line.startswith(("CapEff:", "CapPrm:")) and int(line.split()[1], 16):
            raise SandboxSetupError("Sandbox retained effective/permitted capabilities")
    apply_seccomp(NetworkPolicy(payload["network"]))
    child = os.fork()
    if child == 0:
        try:
            os.execvpe(payload["argv"][0], payload["argv"], dict(os.environ))
        except OSError as exc:
            print(f"Cannot execute command: {exc}", file=sys.stderr, flush=True)
            os._exit(127)

    # 将宿主的终止信号传给目标进程，命名空间 init 负责最终回收孤儿进程
    def forward(signum: int, frame: object) -> None:
        try:
            os.kill(child, signum)
        except ProcessLookupError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, forward)
    while True:
        pid, status_code = os.waitpid(-1, 0)
        if pid == child:
            code = os.waitstatus_to_exitcode(status_code)
            return code if code >= 0 else 128 - code


# 外层先准备文件系统与命名空间，再进入内层安装 seccomp
def _outer(payload: dict[str, Any]) -> int:
    load_seccomp()  # 在创建命名空间前给出依赖缺失诊断
    policy = SandboxPolicy(FileSystemPolicy(
        read_roots=tuple(Path(p) for p in payload["read_roots"]),
        write_roots=tuple(Path(p) for p in payload["write_roots"]),
        protected_paths=tuple(Path(p) for p in payload["protected_paths"]),
        deny_paths=tuple(Path(p) for p in payload["deny_paths"]),
        include_platform_defaults=payload["include_platform_defaults"],
    ), NetworkPolicy(payload["network"]))
    handles = MountHandles()
    try:
        entry = Path(__file__).with_name("__main__.py").resolve()
        runtime = (Path(sys.base_prefix).resolve(), Path(sys.executable).absolute().parent,
                   entry.parents[4] / "kama_claude")
        args = build_bwrap_args(policy, Path(payload["cwd"]), handles.pin, runtime_roots=runtime)
        # 被保护或拒绝的覆盖挂载不再与原可写挂载 inode 相同，只校验仍可见的挂载
        hidden = policy.file_system.deny_paths
        payload["identities"] = [
            identity for identity in handles.identities
            if not any(Path(identity[0]).is_relative_to(root) for root in hidden)
        ]
        payload["mount_fds"] = handles.descriptors
        inner = [sys.executable, "-I", "-S", str(entry), "--inner", json.dumps(payload)]
        os.execve(payload["bwrap"], [payload["bwrap"], *args, "--", *inner], dict(os.environ))
    finally:
        handles.close()
    return 125


# 解析辅助进程协议并把所有初始化错误转换为不可重试的统一诊断
def main() -> int:
    parser = argparse.ArgumentParser()
    stage = parser.add_mutually_exclusive_group(required=True)
    stage.add_argument("--outer")
    stage.add_argument("--inner")
    args = parser.parse_args()
    try:
        payload = json.loads(args.outer or args.inner)
        return _outer(payload) if args.outer else _inner(payload)
    except (SandboxError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"{SETUP_ERROR_PREFIX} {exc}", file=sys.stderr, flush=True)
        return 125
