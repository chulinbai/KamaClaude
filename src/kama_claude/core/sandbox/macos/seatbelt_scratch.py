from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from ..models import SandboxPolicy


# 为单次命令创建私有临时目录，避免允许写入整个用户临时目录
@contextmanager
def process_scratch(
    policy: SandboxPolicy, env: dict[str, str],
) -> Iterator[tuple[SandboxPolicy, dict[str, str]]]:
    with tempfile.TemporaryDirectory(prefix="kama-sandbox-") as directory:
        root = Path(directory).resolve()
        os.chmod(root, 0o700)
        fs = replace(policy.file_system, write_roots=policy.file_system.write_roots + (root,))
        yield replace(policy, file_system=fs), {**env, "TMPDIR": str(root)}
