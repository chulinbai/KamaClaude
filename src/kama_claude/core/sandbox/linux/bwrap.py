from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..models import NetworkPolicy, SandboxPolicy

PLATFORM_READ_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")


# 按只读基线、可写授权、保护项、拒绝项的顺序生成挂载与命名空间参数
def build_bwrap_args(
    policy: SandboxPolicy, cwd: Path, pin: Callable[[Path], str],
    *, runtime_roots: tuple[Path, ...] = (),
) -> list[str]:
    fs = policy.file_system
    args = [
        "--die-with-parent", "--unshare-user", "--disable-userns", "--unshare-pid",
        "--unshare-ipc", "--unshare-uts", "--cap-drop", "ALL",
    ]
    if policy.network == NetworkPolicy.DENY:
        args.append("--unshare-net")
    if Path("/") in fs.read_roots:
        args += ["--ro-bind", "/", "/"]
    else:
        args += ["--tmpfs", "/"]
    args += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    if Path("/") not in fs.read_roots:
        roots = set(fs.read_roots + fs.write_roots + runtime_roots)
        if fs.include_platform_defaults:
            roots.update(Path(p) for p in PLATFORM_READ_ROOTS if Path(p).exists())
        # 保留系统 /bin、/lib 等只读别名，确保可执行文件的动态加载器可用
        for root in sorted(roots, key=lambda p: (len(p.parts), str(p))):
            target = root.resolve()
            args += ["--ro-bind", pin(target), str(target)]
            if target != root:
                args += ["--symlink", str(target), str(root)]
    for root in fs.write_roots:
        args += ["--bind", pin(root), str(root)]
    for root in fs.protected_paths:
        args += ["--ro-bind", pin(root), str(root)]
    for root in fs.deny_paths:
        if root.is_dir():
            args += ["--perms", "0000", "--tmpfs", str(root), "--remount-ro", str(root)]
        else:
            args += ["--ro-bind", "/dev/null", str(root)]
    if Path("/") not in fs.read_roots:
        args += ["--remount-ro", "/"]
    args += ["--chdir", str(cwd), "--setenv", "TMPDIR", "/tmp"]
    return args
