"""ASGI 入口：uvicorn skillos.api.asgi:app。

与 main.py 分离：避免 import main 即触发默认引擎/建库（测试只 import create_app）。
"""

from __future__ import annotations

from skillos.api.main import create_app

app = create_app()
