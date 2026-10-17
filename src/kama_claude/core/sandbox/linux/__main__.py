from __future__ import annotations

import sys
from pathlib import Path

# 使用安装位置而不是 cwd 或 PYTHONPATH 加载辅助程序；-I -S 禁止启动钩子。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from kama_claude.core.sandbox.linux.runner import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
