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

    def test_no_mount_without_env(self, tmp_path, monkeypatch) -> None:
        """本地开发（vite 5173）不设 SKILLOS_WEB_DIST → 根路径不托管。"""
        monkeypatch.delenv("SKILLOS_WEB_DIST", raising=False)
        app = create_app(db_url=f"sqlite:///{tmp_path / 'c2.db'}", jwt_secret=SECRET)
        assert TestClient(app).get("/").status_code == 404


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
