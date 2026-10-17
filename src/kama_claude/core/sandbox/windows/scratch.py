from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from ..errors import SandboxCleanupError
from ..models import SandboxPolicy
from ..policy import normalize_path


@contextmanager
def process_scratch(
    policy: SandboxPolicy, env: dict[str, str],
) -> Iterator[tuple[SandboxPolicy, dict[str, str]]]:
    directory = tempfile.TemporaryDirectory(prefix="kama-sandbox-")
    try:
        root = normalize_path(Path(directory.name), Path.cwd())
        fs = policy.file_system
        scratch_policy = replace(policy, file_system=replace(
            fs, write_roots=fs.write_roots + (root,),
        ))
        clean = {k: v for k, v in env.items() if k.upper() not in ("TEMP", "TMP")}
        yield scratch_policy, {**clean, "TEMP": str(root), "TMP": str(root)}
    finally:
        try:
            directory.cleanup()
        except OSError as exc:
            raise SandboxCleanupError("Cannot remove Windows sandbox temporary directory") from exc
