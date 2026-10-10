"""CLI 单角色入口（`shared_runtime=false` 时按 target 独立构建用）。

默认发行走 skillos_main.py 的 multicall 形态；本文件保留给「三个角色
各自 standalone」的退路 —— 详见 build/nuitka.json 的 `shared_runtime`。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main() -> None:
    from skillos.cli.main import app

    app()


if __name__ == "__main__":
    main()
