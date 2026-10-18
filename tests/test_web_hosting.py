"""线上 Web 托管（04 §1.4 容器 A）：SPA 静态挂载的正确性。

覆盖三类**真实会发生的线上故障**（都不是风格问题）：

1. `mount("/")` 注册顺序 —— Starlette 的 `Mount` 对空路径（`/`）会匹配
   **所有** http / websocket scope，路由匹配是线性按注册顺序。若静态挂载
   排在 `/ws/*` 之前，daemon 与 UI 都连不上云（沙盒演示的核心链路直接断）。
2. `StaticFiles(html=True)` **没有 SPA fallback** —— 它只在目录请求时给
   index.html；刷新 `/skills/:slug` 返回 404。前端路由形同虚设。
3. SPA fallback 不能过激 —— `/api/*` 的 404 必须保持统一响应包装的 JSON
   语义，不能被吞成 HTML（否则前端错误处理全部失效）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skillos.api.main import create_app
from skillos.api.security import issue_jwt

SECRET = "test-secret"


@pytest.fixture()
def dist(tmp_path):
    """模拟 vite build 产物（index.html + 带哈希的静态资源）。"""
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text(
        "<!doctype html><div id=root>SKILLOS_SPA</div>", encoding="utf-8"
    )
    (d / "assets" / "app-abc123.js").write_text("console.log(1)", encoding="utf-8")
    return d


@pytest.fixture()
def client(tmp_path, monkeypatch, dist):
    monkeypatch.setenv("SKILLOS_WEB_DIST", str(dist))
    app = create_app(db_url=f"sqlite:///{tmp_path / 'cloud.db'}", jwt_secret=SECRET)
    return TestClient(app), app


class TestSpaServing:
    def test_root_serves_index(self, client) -> None:
        c, _ = client
        r = c.get("/")
        assert r.status_code == 200
        assert "SKILLOS_SPA" in r.text

    def test_hashed_asset_served(self, client) -> None:
        c, _ = client
        r = c.get("/assets/app-abc123.js")
        assert r.status_code == 200
        assert r.text == "console.log(1)"

    def test_spa_fallback_for_client_route(self, client) -> None:
        """/skills/:slug 刷新必须回到 index.html（否则前端路由直接 404）。"""
        c, _ = client
        r = c.get("/skills/demo-skill")
        assert r.status_code == 200
        assert "SKILLOS_SPA" in r.text

    def test_spa_fallback_keeps_missing_asset_404(self, client) -> None:
        """带扩展名的资源缺失仍应 404 —— 回退成 HTML 会掩盖构建产物缺失。"""
        c, _ = client
        r = c.get("/assets/missing-xyz.js")
        assert r.status_code == 404

    def test_api_404_stays_json(self, client) -> None:
        """/api/* 的 404 必须保持 JSON 包装，不得被 SPA fallback 吞掉。"""
        c, _ = client
        r = c.get("/api/definitely-not-a-route")
        assert r.status_code == 404
        assert "SKILLOS_SPA" not in r.text
        assert r.headers["content-type"].startswith("application/json")

    def test_unmatched_write_is_404_not_405(self, client) -> None:
        """非 GET 的未注册路径 → 404（静态托管不能把它变成 405 Allow: GET,HEAD）。

        回归用例：沙盒关闭时 `POST /api/sandbox/reset` 应当 404，
        挂载 SPA 后曾被 StaticFiles 的 405 顶掉（2026-10-17）。
        """
        c, _ = client
        r = c.post("/api/definitely-not-a-route", json={})
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")

    def test_wrong_method_on_existing_route_is_405(self, client) -> None:
        """路径存在但方法不对 → 仍是 405（不能一律降级成 404）。"""
        c, _ = client
        r = c.post("/api/health")
        assert r.status_code == 405
        assert "GET" in r.headers.get("allow", "")

    def test_no_mount_when_nothing_to_mount(self, tmp_path, monkeypatch) -> None:
        """没有任何可挂载产物（vite 5173 开发态）→ 根路径不托管，照旧 404。

        2026-10-17 语义收紧：以前是「没设 SKILLOS_WEB_DIST 就不挂载」，导致装好包
        直接起服务的人首页 404。现在改为「按候选顺序自动探测，探测不到才不挂载」，
        本用例通过清空候选来模拟"确实没有产物"。
        """
        from skillos.api import web

        monkeypatch.delenv("SKILLOS_WEB_DIST", raising=False)
        monkeypatch.setattr(web, "_candidate_dirs", lambda: [])
        app = create_app(db_url=f"sqlite:///{tmp_path / 'c2.db'}", jwt_secret=SECRET)
        assert TestClient(app).get("/").status_code == 404

    def test_env_var_is_sole_candidate(self, tmp_path, monkeypatch) -> None:
        """显式给 SKILLOS_WEB_DIST 时不做任何回退：路径里没有 index.html 就报错。"""
        from skillos.api import web

        empty = tmp_path / "empty-dist"
        empty.mkdir()
        monkeypatch.setenv("SKILLOS_WEB_DIST", str(empty))
        assert web._candidate_dirs() == [empty]
        dist, reason = web.resolve_web_dist()
        assert dist is None and "index.html" in reason

    def test_auto_detect_from_repo_layout(self, tmp_path, monkeypatch) -> None:
        """无环境变量时自动发现 `<repo>/web/dist`（源码 / editable 安装形态）。"""
        from skillos.api import web

        monkeypatch.delenv("SKILLOS_WEB_DIST", raising=False)
        repo = tmp_path / "repo"
        (repo / "skillos" / "api").mkdir(parents=True)
        (repo / "web" / "dist").mkdir(parents=True)
        (repo / "web" / "dist" / "index.html").write_text("spa", encoding="utf-8")
        monkeypatch.setattr(web, "__file__", str(repo / "skillos" / "api" / "web.py"))
        dist, source = web.resolve_web_dist()
        assert dist == repo / "web" / "dist"
        assert source == "auto"

    def test_source_tree_prefers_local_build_over_bundled(
        self, tmp_path, monkeypatch
    ) -> None:
        """源码树里 `web/dist` 必须压过 `skillos/web_dist`（2026-10-17 实测踩坑）。

        `skillos/web_dist` 是 `pip wheel` 的构建副产物，打完包就留在树里不再更新。
        若它排在前面，`npm run build` 出的新产物永远被旧快照挡住 —— 现象是
        「重建了前端，页面还是老版本、assets 哈希没变」。
        """
        from skillos.api import web

        monkeypatch.delenv("SKILLOS_WEB_DIST", raising=False)
        repo = tmp_path / "repo"
        (repo / "skillos" / "api").mkdir(parents=True)
        (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        # 构建副产物（旧）
        (repo / "skillos" / "web_dist").mkdir(parents=True)
        (repo / "skillos" / "web_dist" / "index.html").write_text(
            "old", encoding="utf-8"
        )
        # 本地新构建
        (repo / "web" / "dist").mkdir(parents=True)
        (repo / "web" / "dist" / "index.html").write_text("new", encoding="utf-8")
        monkeypatch.setattr(web, "__file__", str(repo / "skillos" / "api" / "web.py"))
        dist, _ = web.resolve_web_dist()
        assert dist == repo / "web" / "dist"
        # 反过来：装机形态（无 pyproject.toml）随包产物优先
        (repo / "pyproject.toml").unlink()
        dist, _ = web.resolve_web_dist()
        assert dist == repo / "skillos" / "web_dist"

    def test_frontend_source_dir_is_never_a_candidate(
        self, tmp_path, monkeypatch
    ) -> None:
        """源码根 `web/` 有 vite 源模板 index.html，绝不能被当成产物挂上去。

        真实现场：新克隆仓库还没 `npm run build` 时，候选表里那条
        `pkg_dir.parent / "web"`（本意是发行包的 runtime/web）正好命中
        `web/`，`index.html` 判据照样通过 → 浏览器白屏、`/src/main.tsx` 404，
        而不是「前端未构建」的提示。判据是目录里有 package.json / src/。
        """
        from skillos.api import web

        monkeypatch.delenv("SKILLOS_WEB_DIST", raising=False)
        repo = tmp_path / "repo"
        (repo / "skillos" / "api").mkdir(parents=True)
        (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        # vite 源模板：index.html 引 /src/main.tsx，旁边有 package.json 与 src/
        (repo / "web" / "src").mkdir(parents=True)
        (repo / "web" / "package.json").write_text("{}", encoding="utf-8")
        (repo / "web" / "index.html").write_text(
            '<script type="module" src="/src/main.tsx"></script>', encoding="utf-8"
        )
        monkeypatch.setattr(web, "__file__", str(repo / "skillos" / "api" / "web.py"))

        assert repo / "web" not in web._candidate_dirs()
        dist, reason = web.resolve_web_dist()
        assert dist is None
        assert "npm run build" in reason

    def test_explicit_env_pointing_at_source_dir_is_rejected(
        self, tmp_path, monkeypatch
    ) -> None:
        """显式给源码目录要报错，而不是"挂上去看起来成功"。

        显式路径不参与回退（hard-miss），但也不能因为"有 index.html"就放行 ——
        那等于把白屏伪装成可用。
        """
        from skillos.api import web

        src = tmp_path / "web"
        (src / "src").mkdir(parents=True)
        (src / "package.json").write_text("{}", encoding="utf-8")
        (src / "index.html").write_text("<div id=root></div>", encoding="utf-8")
        monkeypatch.setenv("SKILLOS_WEB_DIST", str(src))

        dist, reason = web.resolve_web_dist()
        assert dist is None
        assert "源码目录" in reason


class TestWebSocketReachability:
    def test_mount_registered_after_websocket_routes(self, client) -> None:
        """顺序即语义：Mount("/") 一旦排在 WS 路由之前就会吞掉它们。"""
        _, app = client
        names = [type(r).__name__ for r in app.routes]
        mount_at = names.index("Mount")
        ws_at = [i for i, n in enumerate(names) if n == "APIWebSocketRoute"]
        assert ws_at, "预期至少注册 /ws/agent 与 /ws/ui"
        assert mount_at > max(ws_at), (
            f"静态挂载(index={mount_at}) 必须在所有 WS 路由之后，实际 names={names}"
        )

    def test_ws_ui_reachable_when_web_mounted(self, client) -> None:
        """托管静态文件后 /ws/ui 仍可握手（未带 token 会被服务端拒绝，不是路由丢失）。"""
        c, _ = client
        token = issue_jwt("u1", SECRET)
        with c.websocket_connect(f"/ws/ui?token={token}") as ws:
            ws.close()
