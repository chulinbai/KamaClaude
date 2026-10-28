from __future__ import annotations

import argparse

from kama_claude.core.limits import normalize_max_steps


# 校验命令行步数并保留零值，以便明确覆盖 Core 的有限上限
def parse_max_steps(value: str) -> int:
    try:
        parsed = int(value)
        normalize_max_steps(parsed)
        return parsed
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--max-steps requires a non-negative integer (0 means unlimited)"
        ) from exc
