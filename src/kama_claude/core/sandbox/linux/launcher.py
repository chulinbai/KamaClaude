from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from ..errors import SandboxUnavailableError

_REQUIRED_FLAGS = (
    "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-net",
    "--die-with-parent", "--cap-drop", "--perms", "--disable-userns",
)


# 仅接受管理员拥有且普通用户不能替换的系统 Bubblewrap
def find_bwrap() -> Path:
    for candidate in (Path("/usr/bin/bwrap"), Path("/bin/bwrap")):
        path = candidate.resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        if any(
            item.stat().st_uid != 0 or stat.S_IMODE(item.stat().st_mode) & 0o022
            for item in (path, *path.parents)
        ):
            continue
        try:
            result = subprocess.run(
                [str(path), "--help"], capture_output=True, text=True, timeout=5,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailableError(f"Cannot probe Bubblewrap: {exc}") from exc
        if result.returncode == 0 and all(flag in result.stdout for flag in _REQUIRED_FLAGS):
            return path
    raise SandboxUnavailableError(
        "A trusted system Bubblewrap with --disable-userns support is required; "
        "install or upgrade bubblewrap. No unsandboxed fallback is allowed."
    )
