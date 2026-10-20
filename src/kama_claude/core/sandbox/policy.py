from __future__ import annotations

import os
import stat
import sys
from dataclasses import replace
from pathlib import Path

from .errors import SandboxPolicyError
from .models import FileSystemPolicy, NetworkPolicy, SandboxPolicy, WindowsSandboxConfig

PROTECTED_NAMES = (".git", ".agents", ".codex", ".aws", ".kama")
_SYSTEM_ALIASES = {Path("/tmp"), Path("/var"), Path("/etc")}


# 拒绝 C0、DEL 与 C1 控制字符，并在错误中转义路径以保留原始值
def _validate_path_characters(path: Path) -> None:
    text = str(path)
    for character in text:
        codepoint = ord(character)
        if codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            raise SandboxPolicyError(
                f"Sandbox path contains control character U+{codepoint:04X}: {text!r}"
            )


# 将策略路径绝对化，并拒绝可被调用方替换的符号链接组件
def normalize_path(path: Path, cwd: Path) -> Path:
    _validate_path_characters(path)
    _validate_path_characters(cwd)
    expanded = path.expanduser()
    _validate_path_characters(expanded)
    if os.name == "nt":
        from .windows.policy import validate_windows_path

        validate_windows_path(str(expanded))
        validate_windows_path(str(cwd))
    absolute = Path(os.path.abspath(cwd / expanded))
    _validate_path_characters(absolute)
    for component in (*reversed(absolute.parents), absolute):
        if component.is_symlink():
            try:
                target = component.resolve()
            except (OSError, RuntimeError) as exc:
                raise SandboxPolicyError(f"Cannot resolve sandbox symlink: {component}") from exc
            _validate_path_characters(target)
            if component not in _SYSTEM_ALIASES or target != Path("/private") / component.name:
                raise SandboxPolicyError(f"Sandbox policy crosses a symlink: {component}")
        if os.name == "nt":
            try:
                attributes = getattr(component.lstat(), "st_file_attributes", None)
            except FileNotFoundError:
                continue
            if attributes is None or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise SandboxPolicyError(f"Sandbox policy crosses a reparse point: {component}")
    try:
        resolved = absolute.resolve()
    except (OSError, RuntimeError) as exc:
        raise SandboxPolicyError(f"Cannot resolve sandbox path: {absolute}") from exc
    _validate_path_characters(resolved)
    return resolved


# 去重并按路径深度排序，使后端稳定地从父目录处理到子目录
def _ordered(paths: list[Path]) -> tuple[Path, ...]:
    return tuple(sorted(set(paths), key=lambda p: (len(p.parts), str(p))))


# 把工作区与显式资源权限转换为后端可执行的绝对路径策略
def normalize_policy(policy: SandboxPolicy, cwd: Path) -> SandboxPolicy:
    _validate_path_characters(cwd)
    try:
        network = NetworkPolicy(policy.network)
    except ValueError as exc:
        raise SandboxPolicyError("Sandbox network policy must be allow or deny") from exc
    fs = policy.file_system
    reads = [normalize_path(p, cwd) for p in fs.read_roots]
    writes = [normalize_path(p, cwd) for p in fs.write_roots]
    protected = [normalize_path(p, cwd) for p in fs.protected_paths]
    denied = [normalize_path(p, cwd) for p in fs.deny_paths]
    for path in reads + writes:
        if not path.exists():
            raise SandboxPolicyError(f"Sandbox grant does not exist: {path}")
        if not path.is_dir() and not path.is_file():
            raise SandboxPolicyError(f"Sandbox grants require a file or directory: {path}")
    for path in writes:
        if path == Path(path.anchor):
            raise SandboxPolicyError("Writing the filesystem root is not supported")
        for name in PROTECTED_NAMES:
            candidate = path / name
            if candidate.exists() or candidate.is_symlink():
                protected.append(normalize_path(candidate, cwd))
    # 缺失限制目标收紧到最近的现有祖先，避免挂载时在宿主创建占位文件
    for paths in (protected, denied):
        for index, path in enumerate(paths):
            while not path.exists() and path.parent != path:
                path = path.parent
            paths[index] = path
    return replace(policy, network=network, file_system=FileSystemPolicy(
        read_roots=_ordered(reads), write_roots=_ordered(writes),
        protected_paths=_ordered(protected), deny_paths=_ordered(denied),
        include_platform_defaults=fs.include_platform_defaults,
    ))


# 创建默认策略：当前工作区可读写，系统运行资源由各沙箱后端添加
def workspace_policy(workspace: Path) -> SandboxPolicy:
    root = normalize_path(workspace, Path.cwd())
    return SandboxPolicy(FileSystemPolicy(read_roots=(root,), write_roots=(root,)))


# 为原有策略补充宿主运行代码保护，文件工具和 Shell 使用相同禁止项
def protect_runtime(policy: SandboxPolicy) -> SandboxPolicy:
    runtime_paths = (
        Path(__file__).resolve().parents[2], Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(), Path(sys.executable).resolve(),
    )
    return replace(policy, file_system=replace(
        policy.file_system, protected_paths=policy.file_system.protected_paths + runtime_paths,
    ))


# 把跨平台配置与兼容的 Windows 配置合并到现有文件系统策略
def configured_policy(
    workspace: Path, filesystem: FileSystemPolicy | None = None,
    windows: WindowsSandboxConfig | None = None,
) -> SandboxPolicy:
    fs = filesystem or FileSystemPolicy(read_roots=())
    policy = workspace_policy(workspace)
    combined = replace(
        fs, read_roots=policy.file_system.read_roots + fs.read_roots,
        write_roots=policy.file_system.write_roots + fs.write_roots,
    )
    if sys.platform == "win32" and windows is not None:
        combined = replace(
            combined, read_roots=combined.read_roots + windows.readonly_paths,
            write_roots=combined.write_roots + windows.readwrite_paths,
            deny_paths=combined.deny_paths + windows.denied_paths,
        )
        policy = replace(policy, network=windows.network)
    return protect_runtime(replace(policy, file_system=combined))


# 合并调用方已经审批通过的额外授权，同时保留原有禁止项
def with_approved_permissions(
    policy: SandboxPolicy, *, read_roots: tuple[Path, ...] = (),
    write_roots: tuple[Path, ...] = (),
) -> SandboxPolicy:
    fs = policy.file_system
    return replace(policy, file_system=replace(
        fs, read_roots=fs.read_roots + read_roots, write_roots=fs.write_roots + write_roots,
    ))


# 清理会在辅助进程建立隔离前注入代码的环境变量
def clean_environment(env: dict[str, str]) -> dict[str, str]:
    blocked = {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "GCONV_PATH", "LOCPATH"}
    return {
        key: value for key, value in env.items()
        if key not in blocked
        and not key.startswith(("LD_", "DYLD_", "PYTHON", "BASH_FUNC_"))
    }
