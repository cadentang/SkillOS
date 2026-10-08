"""Web 静态托管（04 §1.4 容器 A）：FastAPI 与 SPA 构建产物同进程。

为什么单独成模块而不是在 main.py 内联三行：

1. **SPA fallback 必须自己实现** —— Starlette 的 `StaticFiles(html=True)` 只在
   请求目录时给 `index.html`；前端路由（`/skills/:slug`）刷新会 404。
2. **回退必须收敛** —— `/api/*` 的 404 是统一响应包装的 JSON 契约，不能变成
   HTML；带扩展名的资源缺失也必须照旧 404（否则构建产物缺失被掩盖成白屏）。
3. **挂载顺序是语义** —— `Mount("/")` 在 Starlette 中对空路径会匹配**所有**
   http / websocket scope，而路由匹配严格按注册顺序。因此它必须排在
   `/api/*`、`/ws/agent`、`/ws/ui` 之后，否则 WS 全被 StaticFiles 吃掉
   （`assert scope["type"] == "http"` 直接崩）。顺序约束由 tests/test_web_hosting.py 守住。
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

# 这些前缀下的 404 保持 JSON 语义（统一响应包装），不做 SPA 回退
RESERVED_PREFIXES = ("api/", "ws/")


class SpaStaticFiles(StaticFiles):
    """静态资源 + SPA 回退：未命中的**无扩展名**路径回退 index.html。

    回退判据用「无扩展名」而非「非 api/ws 前缀」为主，是因为前者同时覆盖了
    `/skills/demo`（前端路由）与 `/assets/x.js`（构建产物）两种情况；后者只防
    前缀穿透。两者都要。

    `path` 参数由 `StaticFiles.get_path` 经 `os.path.normpath` 处理过 ——
    在 Windows 上分隔符已被换成 `\\`，直接 `startswith("api/")` 会静默失效
    （本机开发态把 `/api/*` 的 404 吞成 index.html）。统一按 POSIX 归一化
    （05 跨平台 P0-4 的同一条教训：不依赖平台默认的路径行为）。
    """

    async def get_response(self, path: str, scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            norm = path.lstrip("/").lstrip("\\").replace("\\", "/")
            if norm.startswith(RESERVED_PREFIXES) or norm in ("api", "ws"):
                raise
            # 带扩展名 = 找的是具体资源，缺失就是缺失，不能伪装成 index.html
            if PurePosixPath(norm).suffix:
                raise
            return await super().get_response("index.html", scope)


def mount_web(app: FastAPI) -> bool:
    """按 `SKILLOS_WEB_DIST` 挂载 SPA；返回是否挂载。

    **调用时机**：所有路由（含 WS）注册完成之后 —— 见模块 docstring 第 3 条。
    未设置环境变量（本地 vite 5173 开发态）则完全不挂载。
    """
    web_dist = os.environ.get("SKILLOS_WEB_DIST")
    if not web_dist or not Path(web_dist).is_dir():
        return False
    app.mount("/", SpaStaticFiles(directory=web_dist, html=True), name="web")
    return True
