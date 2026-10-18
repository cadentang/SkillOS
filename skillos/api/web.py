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
import sys
from pathlib import Path, PurePosixPath

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from skillos.core.logging import get_logger

log = get_logger("skillos.api.web")

# 这些前缀下的 404 保持 JSON 语义（统一响应包装），不做 SPA 回退
RESERVED_PREFIXES = ("api/", "ws/")

# `SKILLOS_WEB_DIST` 取这些值时视为"显式关闭托管"（`skillos serve --dev`：
# 前端由 vite 5173 提供，后端不该再把 / 抢走）。用哨兵而不是空串，是因为
# 空串在环境变量里和"没设"不可区分 —— 那会静默回退到自动探测。
_DIST_DISABLED = frozenset({"off", "0", "none", "false"})


class SpaStaticFiles(StaticFiles):
    """静态资源 + SPA 回退：未命中的**无扩展名**路径回退 index.html。

    回退判据用「无扩展名」而非「非 api/ws 前缀」为主，是因为前者同时覆盖了
    `/skills/demo`（前端路由）与 `/assets/x.js`（构建产物）两种情况；后者只防
    前缀穿透。两者都要。

    `path` 参数由 `StaticFiles.get_path` 经 `os.path.normpath` 处理过 ——
    在 Windows 上分隔符已被换成 `\\`，直接 `startswith("api/")` 会静默失效
    （本机开发态把 `/api/*` 的 404 吞成 index.html）。统一按 POSIX 归一化
    （05 跨平台 P0-4 的同一条教训：不依赖平台默认的路径行为）。

    **非 GET/HEAD 请求不能进静态托管**（2026-10-17 修复）：`Mount("/")` 对
    任意方法都匹配，而 StaticFiles 对非读方法一律抛 405 —— 于是「未注册的
    POST 路径」得到 405（Allow: GET, HEAD）而不是统一的 404 JSON。这里改为
    先查 app 路由表：路径存在但方法不对 → 405（带正确的 Allow），路径不存在
    → 404，交回应用的错误包装。
    """

    def __init__(self, *args, app: FastAPI | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._app = app

    def _allowed_methods(self, path: str) -> set[str]:
        """app 里与 path 完全匹配的路由允许的方法集合（路径参数路由不参与）。"""
        allowed: set[str] = set()
        for route in (self._app.routes if self._app is not None else []):
            rp = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if not rp or not methods:
                continue
            if rp.rstrip("/") == path:
                allowed |= set(methods)
        return allowed

    async def get_response(self, path: str, scope) -> Response:
        method = str(scope.get("method") or "GET").upper()
        if method not in ("GET", "HEAD"):
            full = "/" + str(scope.get("path") or "").lstrip("/")
            allowed = self._allowed_methods(full)
            if allowed:
                raise HTTPException(
                    status_code=405, headers={"Allow": ", ".join(sorted(allowed))}
                )
            raise HTTPException(status_code=404)
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


# 前端源码根的标志文件/目录。构建产物目录里不会有这些 —— 编译发行包的
# `runtime/web` 只有 `index.html` + `assets/`。
_FRONTEND_SOURCE_MARKERS = ("package.json", "vite.config.ts", "vite.config.js")


def _is_frontend_source_dir(d: Path) -> bool:
    """判断目录是「前端源码根」而不是「构建产物目录」。

    **为什么必须排除**（2026-10-17）：候选表里有一条 `pkg_dir.parent / "web"`，
    本意是编译发行包的 `runtime/web`。但在源码态它正好指向仓库的 `web/` ——
    那里有 vite 的源模板 `index.html`（引用 `/src/main.tsx`），"index.html 存在"
    这条判据照样通过。于是新克隆、还没 `npm run build` 时会挂上源模板：浏览器
    白屏、`/src/main.tsx` 404，而不是"前端未构建"的明确提示。
    """
    return (d / "src").is_dir() or any(
        (d / m).is_file() for m in _FRONTEND_SOURCE_MARKERS
    )


def _candidate_dirs() -> list[Path]:
    """按优先级列出可能的 SPA 构建产物目录（全部为绝对路径）。

    为什么要多候选而不是只认环境变量（2026-10-17 修复）：装好包直接
    `uvicorn skillos.api.asgi:app` 的用户拿不到 SKILLOS_WEB_DIST，首页
    404 且无任何提示。四种部署形态的产物落点本来就不同：

    1. 环境变量 `SKILLOS_WEB_DIST`（显式指定，最高优先级；取 `off` 即关闭托管）
    2. `<repo>/web/dist`       —— 源码 / editable 安装（本仓库开发态）
    3. `skillos/web_dist`      —— wheel 安装时随包携带（构建期从 web/dist 拷入）
    4. `runtime/web`           —— 编译发行包（build.py 把前端拷到 runtime/web；
                                  模块路线从 runtime/skillos 上一级找到，
                                  二进制路线从可执行文件同级找到）

    **2 与 3 的顺序不能反**（2026-10-17 实测踩坑）：`skillos/web_dist` 是
    `pip wheel` / `setup.py` 的**构建副产物**，打完包就留在源码树里不再更新。
    它若排在前面，源码态 `npm run build` 出的新产物永远被这份旧快照挡住 ——
    表现为「重建了前端但页面还是老版本、assets 哈希没变」。所以源码态必须以
    本地 `web/dist` 为准；只有装出来的包（`site-packages/skillos` 旁边没有
    pyproject.toml）才该用随包的 `web_dist`。判据用「上级目录有 pyproject.toml」
    来区分源码树与安装目录 —— editable 安装的 `__file__` 就在仓库里，同属源码态。
    """
    pkg_dir = Path(__file__).resolve().parent.parent  # .../skillos
    repo_root = pkg_dir.parent
    env = os.environ.get("SKILLOS_WEB_DIST")
    if env:
        if env.strip().lower() in _DIST_DISABLED:
            return []  # 显式关闭（serve --dev）：前端另有来源，别抢 /
        # 显式指定即唯一候选：路径错了就报错，**不静默回退**到自动探测
        # （与 provider 显式路径 hard-miss 不回退同一条原则）
        return [Path(env).expanduser()]
    bundled = pkg_dir / "web_dist"          # 形态 3：随包携带（构建副产物）
    local_build = repo_root / "web" / "dist"  # 形态 2：源码仓库本地构建
    is_source_tree = (repo_root / "pyproject.toml").is_file()
    raw: list[Path] = (
        [local_build, bundled] if is_source_tree else [bundled, local_build]
    )
    # 形态 4：编译发行包的 runtime/web。模块路线下 `__file__` 的深度随
    # Nuitka 打包粒度变化（可能在 runtime/skillos/skillos.pyd，也可能更深），
    # 逐级往上试；二进制路线则取可执行文件同级。
    for anc in list(pkg_dir.parents)[:3]:
        raw.append(anc / "web")
    try:
        raw.append(Path(sys.executable).resolve().parent / "web")
    except OSError:  # pragma: no cover - 极少数沙箱环境取不到可执行文件路径
        pass
    out: list[Path] = []
    for d in raw:  # 去重但保持优先级顺序
        if d in out:
            continue
        # 源码目录不是产物：`<repo>/web` 有 vite 源模板 index.html，会白屏
        if _is_frontend_source_dir(d):
            continue
        out.append(d)
    return out


def resolve_web_dist() -> tuple[Path | None, str]:
    """定位可用的 SPA 目录，返回 (目录, 来源说明)；找不到则 (None, 原因)。

    判据是 `index.html` 真实存在 —— 只判目录存在会把空的 web/dist
    （`npm run build` 前就有 dist 目录的脚手架）误挂成白屏。
    """
    dirs = _candidate_dirs()
    env = os.environ.get("SKILLOS_WEB_DIST")
    if env and env.strip().lower() in _DIST_DISABLED:
        return None, "SKILLOS_WEB_DIST 显式关闭（dev 形态：前端由 vite 5173 提供）"
    if env:
        # 显式路径不参与自动探测，但**仍要校验它是产物而不是源码根** ——
        # 指到 `web/` 会挂上 vite 源模板（引 /src/main.tsx），白屏且难排查。
        # 这不是"回退"，是明确报错（与 hard-miss 不回退同一条原则）。
        target = dirs[0]
        if _is_frontend_source_dir(target):
            return None, (
                f"SKILLOS_WEB_DIST={target} 是前端源码目录（含 package.json/src），"
                "不是构建产物 —— 请指向 dist 目录，或先 npm run build"
            )
        if (target / "index.html").is_file():
            return target, "SKILLOS_WEB_DIST"
        return None, f"SKILLOS_WEB_DIST={target} 下没有 index.html"
    for d in dirs:
        if (d / "index.html").is_file():
            return d, "auto"
    return None, (
        "候选目录均无 index.html（前端未构建？源码 web/ 目录已排除，"
        "请 cd web && npm run build）"
    )


def mount_web(app: FastAPI) -> bool:
    """挂载 SPA 静态托管；返回是否挂载。

    **调用时机**：所有路由（含 WS）注册完成之后 —— 见模块 docstring 第 3 条。
    找不到构建产物（vite 5173 开发态，或没跑 `npm run build`）则不挂载，
    并留一条明确日志说明原因，避免"浏览器打开是 404"的静默失败。
    """
    dist, source = resolve_web_dist()
    if dist is None:
        log.info(
            "web_not_mounted",
            reason=source,
            hint="开发态请用 vite(5173) 或先 `npm run build`；也可显式设 SKILLOS_WEB_DIST",
        )
        return False
    app.mount("/", SpaStaticFiles(directory=str(dist), html=True, app=app), name="web")
    log.info("web_mounted", dist=str(dist), source=source)
    return True
