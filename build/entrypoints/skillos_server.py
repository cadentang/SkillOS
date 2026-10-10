"""ASGI 服务器入口（编译发行专用）。

为什么不直接 `uvicorn skillos.api.asgi:app`
------------------------------------------
`uvicorn <模块:属性>` 是**字符串解析**入口：Nuitka 静态分析看不到它，
编译后必然 `ImportError`。所以编译发行必须有一个真实的 Python 入口，
在源码里 `import` 出 app 对象，再交给 `uvicorn.run()`。

顺带把 uvicorn 的三处「字符串加载」显式钉死（loop / http / ws），
一是让编译期依赖收敛（auto 会尝试 httptools，我们不需要为它多背一个
C 扩展），二是消除编译后「运行时才决定用哪个实现」的不确定性。

环境变量（launcher 二进制形态即按此注入，见 manifest.components.backend）：
    SKILLOS_HOST        默认 127.0.0.1
    SKILLOS_PORT        默认 9080
    SKILLOS_LOG_LEVEL   默认 info
    SKILLOS_ACCESS_LOG  "1" 时打开访问日志（默认关闭，避免噪音写日志文件）
    SKILLOS_WEB_DIST    SPA 目录（create_app 读取，见 api/web.py）

不支持 `reload` / `workers`：前者要靠子进程重启、后者要靠多进程，
在冻结产物里都不可靠（Nuitka 多进程需 `--module` 或 multiprocessing 插件），
开发热更请走源码形态 `python -m uvicorn --reload`。
"""

from __future__ import annotations

import os
import sys


def run_server() -> None:
    """按环境变量起 ASGI 服务（阻塞直到收到信号）。"""
    # 05 编码基线：中文 skill 名在 Windows 默认 cp936 控制台必崩。
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    import uvicorn

    from skillos.api.asgi import app  # noqa: PLC0415 —— 延迟到 stdio 就绪后

    host = os.environ.get("SKILLOS_HOST", "127.0.0.1")
    port = int(os.environ.get("SKILLOS_PORT", "9080"))
    level = os.environ.get("SKILLOS_LOG_LEVEL", "info")
    access_log = os.environ.get("SKILLOS_ACCESS_LOG", "") == "1"

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=level,
        loop="asyncio",
        http="h11",
        ws="websockets",
        lifespan="on",
        access_log=access_log,
    )


if __name__ == "__main__":
    run_server()
