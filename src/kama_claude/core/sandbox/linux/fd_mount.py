from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import SandboxSetupError


@dataclass
class MountHandles:
    descriptors: list[int] = field(default_factory=list)
    identities: list[tuple[str, int, int]] = field(default_factory=list)

    # 逐组件以 O_NOFOLLOW 打开挂载源，阻止路径解析期间的符号链接替换
    def pin(self, path: Path) -> str:
        if sys.platform != "linux":
            raise SandboxSetupError("Descriptor mounts require Linux")
        flags = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(path.anchor, flags | os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
                if stat.S_ISLNK(os.fstat(fd).st_mode):
                    raise SandboxSetupError(f"Mount source became a symlink: {path}")
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
                raise SandboxSetupError(f"Invalid mount source: {path}")
            os.set_inheritable(fd, True)
            self.descriptors.append(fd)
            self.identities.append((str(path), info.st_dev, info.st_ino))
            return f"/proc/self/fd/{fd}"
        except BaseException:
            os.close(fd)
            raise

    # 在异常路径释放所有固定挂载来源的文件描述符
    def close(self) -> None:
        for fd in self.descriptors:
            os.close(fd)
        self.descriptors.clear()


# 在隔离完成后验证实际挂载仍指向准备时固定的 inode
def verify_mounts(identities: list[tuple[str, int, int]]) -> None:
    for path, device, inode in identities:
        current = os.stat(path)
        if (current.st_dev, current.st_ino) != (device, inode):
            raise SandboxSetupError(f"Sandbox mount identity changed: {path}")
