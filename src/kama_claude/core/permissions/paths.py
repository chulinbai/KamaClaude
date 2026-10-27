from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from kama_claude.core.sandbox.errors import SandboxError
from kama_claude.core.sandbox.models import SandboxPolicy
from kama_claude.core.sandbox.policy import (
    PROTECTED_NAMES,
    normalize_path,
    normalize_policy,
    protect_runtime,
    with_approved_permissions,
    workspace_policy,
)


class PathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    access: Literal["read", "write"]


class PathGrant(PathRequest):
    kind: Literal["file", "directory"]
    operation: Literal["read", "list", "write", "create_dir", "shell"]


class PathAccessError(PermissionError):
    # 保存不可重试的路径错误分类，供调用层返回给模型
    def __init__(self, message: str, code: str = "permission_denied") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PathExecution:
    controller: PathAccessController
    grants: tuple[PathGrant, ...]


_execution: ContextVar[PathExecution | None] = ContextVar("path_execution", default=None)


# 为单次工具调用安装独立权限快照，结束或取消时恢复原上下文
@contextmanager
def path_execution(
    controller: PathAccessController, grants: tuple[PathGrant, ...],
) -> Iterator[None]:
    token = _execution.set(PathExecution(controller, grants))
    try:
        yield
    finally:
        _execution.reset(token)


class PathAccessController:
    # 复用已有沙箱策略，固定工具工作目录和禁止项
    def __init__(self, cwd: Path | None = None, policy: SandboxPolicy | None = None) -> None:
        self.cwd = normalize_path(cwd or Path.cwd(), Path.cwd())
        base = protect_runtime(policy or workspace_policy(self.cwd))
        self.policy = normalize_policy(base, self.cwd)

    # 读取当前调用的独立授权，直接调用工具时仅使用基础权限
    def grants(self) -> tuple[PathGrant, ...]:
        execution = _execution.get()
        return execution.grants if execution and execution.controller is self else ()

    # 根据路径实际类型匹配文件精确权限或目录子树权限
    @staticmethod
    def _covers(root: Path, path: Path, kind: str) -> bool:
        return path == root or (kind == "directory" and path.is_relative_to(root))

    # 校验规范化路径以及不能被动态授权覆盖的禁止项
    def validate(self, request: PathGrant) -> Path:
        path = normalize_path(Path(request.path), self.cwd)
        fs = self.policy.file_system
        if any(path.is_relative_to(root) for root in fs.deny_paths):
            raise PathAccessError(f"Path is explicitly denied: {path}")
        if request.access == "write":
            if path == Path(path.anchor):
                raise PathAccessError("Writing a filesystem root is not supported")
            if any(path.is_relative_to(root) for root in fs.protected_paths):
                raise PathAccessError(f"Path is protected against writes: {path}")
            if any(part.lower() in PROTECTED_NAMES for part in path.parts):
                raise PathAccessError(f"Path contains a protected directory: {path}")
        if path.exists():
            expected = path.is_dir() if request.kind == "directory" else path.is_file()
            if not expected:
                raise PathAccessError(f"Path type changed or conflicts with request: {path}")
        return path

    # 判断基础权限或已批准的会话与单次权限是否覆盖目标
    def allows(self, request: PathGrant, grants: tuple[PathGrant, ...] = ()) -> bool:
        path = self.validate(request)
        fs = self.policy.file_system
        roots = fs.write_roots if request.access == "write" else fs.read_roots + fs.write_roots
        for root in roots:
            if self._covers(root, path, "directory" if root.is_dir() else "file"):
                return True
        for grant in grants:
            if request.access == "write" and grant.access != "write":
                continue
            root = self.validate(grant)
            if self._covers(root, path, grant.kind):
                return True
        return False

    # 构造带规范化目标和操作类型的审批内容，不创建文件系统对象
    def request(
        self, path: str, access: Literal["read", "write"],
        kind: Literal["file", "directory"],
        operation: Literal["read", "list", "write", "create_dir", "shell"],
    ) -> PathGrant:
        target = normalize_path(Path(path), self.cwd)
        request = PathGrant(path=str(target), access=access, kind=kind, operation=operation)
        self.validate(request)
        return request

    # 从内置文件操作或 Shell 的明确声明中提取待检查路径
    def requirements(self, tool_name: str, params: dict[str, object]) -> tuple[PathGrant, ...]:
        if tool_name == "read_image" and params.get("image_id") is not None:
            return ()
        if tool_name == "bash":
            raw = params.get("required_paths", [])
            if not isinstance(raw, list):
                raise PathAccessError("required_paths must be a list", "schema_error")
            requests = []
            for item in raw:
                declared = PathRequest.model_validate(item)
                path = normalize_path(Path(declared.path), self.cwd)
                request = self.request(
                    str(path), declared.access, "directory" if path.is_dir() else "file", "shell",
                )
                if not path.exists():
                    raise PathAccessError(
                        f"Target does not exist: {path}. Use create_dir or write_file first; "
                        "then request this exact path for Shell. Do not request its parent.",
                        "path_not_ready",
                    )
                requests.append(request)
            return tuple(requests)
        write = tool_name in ("write_file", "create_dir")
        directory = tool_name in ("list_dir", "create_dir")
        operation: Literal["read", "list", "write", "create_dir", "shell"] = {
            "read_file": "read", "read_image": "read", "write_file": "write", "list_dir": "list",
            "create_dir": "create_dir",
        }[tool_name]  # type: ignore[assignment]
        return (self.request(str(params.get("path", ".")), "write" if write else "read",
                             "directory" if directory else "file", operation),)

    # 执行前重新校验当前操作的精确权限，禁止未经过调用层的越界访问
    def check(
        self, path: Path, access: Literal["read", "write"],
        kind: Literal["file", "directory"],
    ) -> Path:
        request = self.request(str(path), access, kind, "write" if access == "write" else "read")
        if not self.allows(request, self.grants()):
            raise PathAccessError(f"Path approval required: {request.path} ({access}, {kind})")
        return Path(request.path)

    # 只将已经存在的精确授权合并进沙箱，不提升到父目录
    def sandbox_policy(self) -> SandboxPolicy:
        reads: list[Path] = []
        writes: list[Path] = []
        for grant in self.grants():
            path = self.validate(grant)
            if path.exists():
                (writes if grant.access == "write" else reads).append(path)
        return with_approved_permissions(
            self.policy, read_roots=tuple(reads), write_roots=tuple(writes),
        )

    # 固定父目录链，缺失父目录只能在已授权范围内逐个创建
    @contextmanager
    def _parent(self, path: Path, *, create: bool = False) -> Iterator[int | None]:
        path = normalize_path(path, self.cwd)
        if os.name == "nt":
            from kama_claude.core.sandbox.windows.path_locks import lock_paths

            with ExitStack() as stack:
                for parent in reversed(path.parents):
                    if not parent.exists():
                        if not create:
                            raise PathAccessError(f"Parent must exist: {parent}", "path_not_ready")
                        self._check_parent(parent)
                        parent.mkdir()
                    stack.enter_context(lock_paths((parent,)))
                normalize_path(path, self.cwd)
                if path.exists():
                    stack.enter_context(lock_paths((path,)))
                yield None
            return
        with ExitStack() as stack:
            fd = os.open(path.anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY"))
            stack.callback(os.close, fd)
            current = Path(path.anchor)
            for name in path.parent.parts[1:]:
                current /= name
                try:
                    child = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY")
                                    | getattr(os, "O_NOFOLLOW"), dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise PathAccessError(f"Parent must exist: {current}", "path_not_ready")
                    self._check_parent(current)
                    os.mkdir(name, dir_fd=fd)
                    child = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY")
                                    | getattr(os, "O_NOFOLLOW"), dir_fd=fd)
                stack.callback(os.close, child)
                fd = child
            normalize_path(path, self.cwd)
            yield fd

    # 缺失的父目录必须已获授权，否则要求单独创建而不扩大当前请求
    def _check_parent(self, path: Path) -> None:
        try:
            self.check(path, "write", "directory")
        except PathAccessError as exc:
            raise PathAccessError(
                f"Missing parent requires separate create_dir approval: {path}", "path_not_ready",
            ) from exc

    # 以禁止跟随链接的方式打开精确文件，写入前检查硬链接与类型
    @contextmanager
    def _file(self, path: Path, *, write: bool = False) -> Iterator[int]:
        path = self.check(path, "write" if write else "read", "file")
        with self._parent(path, create=write) as parent:
            self.check(path, "write" if write else "read", "file")
            flags = os.O_WRONLY | os.O_CREAT if write else os.O_RDONLY
            if os.name == "nt":
                flags |= os.O_BINARY
                if write and not path.exists():
                    flags |= os.O_EXCL
                fd = os.open(path, flags, 0o600)
            else:
                fd = os.open(path.name, flags | getattr(os, "O_NOFOLLOW"), 0o600, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or (write and info.st_nlink > 1):
                    raise PathAccessError(f"Not a regular, independently writable file: {path}")
                if write:
                    os.ftruncate(fd, 0)
                yield fd
            finally:
                os.close(fd)

    # 限量读取获准文件，不加载超出返回上限的完整内容
    def read(self, path: Path, limit: int) -> bytes:
        with self._file(path) as fd, os.fdopen(os.dup(fd), "rb") as stream:
            return stream.read(limit)

    # 写入获准的精确文件，父目录权限与文件权限分别检查
    def write(self, path: Path, content: bytes) -> None:
        with self._file(path, write=True) as fd, os.fdopen(os.dup(fd), "wb") as stream:
            stream.write(content)

    # 仅创建指定目录，不授权或自动创建外部父目录
    def mkdir(self, path: Path) -> Path:
        path = self.check(path, "write", "directory")
        with self._parent(path) as parent:
            self.check(path, "write", "directory")
            try:
                if parent is None:
                    path.mkdir()
                else:
                    os.mkdir(path.name, dir_fd=parent)
            except FileExistsError:
                self.check(path, "write", "directory")
        return path

    # 安全枚举目录，过滤无权访问的条目并拒绝链接递归
    def entries(self, path: Path) -> list[tuple[Path, bool]]:
        path = self.check(path, "read", "directory")
        with self._parent(path) as parent, ExitStack() as stack:
            if parent is None:
                iterator = os.scandir(path)
            else:
                fd = os.open(path.name or path.anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY")
                             | getattr(os, "O_NOFOLLOW"),
                             dir_fd=parent)
                stack.callback(os.close, fd)
                iterator = os.scandir(fd)
            with iterator:
                result = []
                for entry in iterator:
                    target = path / entry.name
                    try:
                        directory = entry.is_dir(follow_symlinks=False)
                        self.check(target, "read", "directory" if directory else "file")
                    except (OSError, ValueError, SandboxError):
                        continue
                    result.append((target, directory))
                return sorted(result, key=lambda item: (not item[1], item[0].name))
