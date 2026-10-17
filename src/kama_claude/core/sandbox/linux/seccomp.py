from __future__ import annotations

import ctypes
import errno
import platform

from ..errors import SandboxSetupError, SandboxUnavailableError
from ..models import NetworkPolicy

_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO = 0x00050000 | errno.EPERM
_COMMON_DENIED = (
    "ptrace", "process_vm_readv", "process_vm_writev", "io_uring_setup", "io_uring_enter",
    "io_uring_register", "mount", "umount2", "pivot_root", "setns", "unshare",
    "open_by_handle_at", "bpf", "keyctl", "add_key", "request_key",
)
_NETWORK_DENIED = (
    "connect", "accept", "accept4", "bind", "listen", "sendto", "sendmmsg",
)


class _ArgCompare(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint), ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64),
    ]


# 在隔离辅助进程中加载系统 libseccomp，缺失时停止执行
def load_seccomp() -> ctypes.CDLL:
    if platform.machine().lower() not in ("x86_64", "amd64", "aarch64", "arm64"):
        raise SandboxUnavailableError("Linux sandbox currently supports x86_64 and aarch64")
    try:
        lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError as exc:
        raise SandboxUnavailableError("System libseccomp.so.2 is required") from exc
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.restype = None
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
        ctypes.POINTER(_ArgCompare),
    ]
    lib.seccomp_rule_add_array.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.restype = ctypes.c_int
    return lib


# 设置不可提权标志并安装本机架构过滤器，其他 ABI 由 libseccomp 默认拒绝
def apply_seccomp(network: NetworkPolicy) -> None:
    lib = load_seccomp()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise SandboxSetupError("Cannot set no_new_privs")
    context = lib.seccomp_init(_SCMP_ACT_ALLOW)
    if not context:
        raise SandboxSetupError("Cannot allocate seccomp filter")

    # 添加单条系统调用拒绝规则，缺少必要调用定义时拒绝启动
    def deny(name: str, comparison: _ArgCompare | None = None) -> None:
        number = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
        if number < 0:
            raise SandboxSetupError(f"libseccomp cannot resolve required syscall: {name}")
        count = 0 if comparison is None else 1
        pointer = None if comparison is None else ctypes.pointer(comparison)
        if lib.seccomp_rule_add_array(context, _SCMP_ACT_ERRNO, number, count, pointer) != 0:
            raise SandboxSetupError(f"Cannot restrict syscall: {name}")

    try:
        for name in _COMMON_DENIED:
            deny(name)
        if network == NetworkPolicy.DENY:
            for name in _NETWORK_DENIED:
                deny(name)
        # socketpair 只允许进程间本地通信；禁止建立能联系宿主守护进程的 Unix socket
        deny("socketpair", _ArgCompare(0, 1, 1, 0))  # SCMP_CMP_NE, AF_UNIX
        if network == NetworkPolicy.DENY:
            deny("socket")
        else:
            # 每条规则只比较一次同一参数，符合 libseccomp 的规则约束
            for family in range(47):
                if family not in (2, 10):  # AF_INET / AF_INET6
                    deny("socket", _ArgCompare(0, 4, family, 0))  # SCMP_CMP_EQ
            deny("socket", _ArgCompare(0, 5, 47, 0))  # SCMP_CMP_GE
        if lib.seccomp_load(context) != 0:
            raise SandboxSetupError("Cannot install seccomp filter")
    finally:
        lib.seccomp_release(context)
