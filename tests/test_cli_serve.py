"""`skillos serve` 与「显式关闭 SPA 托管」契约（2026-10-17）。

两个断言都针对**用户看得见的失败**：

1. `serve` 必须在 `--help` 里出现且带 `--port/--dev/--open` —— 这个命令的
   存在意义就是让 pip 用户不必背 `uvicorn skillos.api.asgi:app` 这条路径；
   参数丢了等于退回原样。
2. `SKILLOS_WEB_DIST=off` 必须真的不挂载 —— `serve --dev` 靠它把 `/` 让给
   vite(5173)。若哨兵失效，后端会抢走 `/`，前端热更看上去"打不开"。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from typer.main import get_command
from typer.testing import CliRunner

from skillos.api.main import create_app
from skillos.cli.main import app

SECRET = "test-secret"


def test_serve_is_registered_with_flags() -> None:
    r = CliRunner().invoke(app, ["serve", "--help"])
    assert r.exit_code == 0, r.output
    # 断言命令对象，不断言渲染文本：typer/rich 按终端宽度排版，80 列下
    # `--host` 可能被折行/省略（2026-10-18 CI 实测：本地过、CI 四平台红）。
    params = {p.name for p in get_command(app).commands["serve"].params}
    assert {"host", "port", "reload", "dev", "open_browser", "db_url"} <= params


def test_serve_appears_in_top_level_help() -> None:
    r = CliRunner().invoke(app, ["--help"])
    assert r.exit_code == 0
    # 同上：help 文本受渲染宽度影响，注册表才是事实源。
    assert "serve" in get_command(app).commands


def test_dev_sentinel_disables_spa_mount(tmp_path, monkeypatch) -> None:
    """`SKILLOS_WEB_DIST=off` → 不挂载 SPA，/api 仍然可用。"""
    monkeypatch.setenv("SKILLOS_WEB_DIST", "off")
    app_ = create_app(db_url=f"sqlite:///{tmp_path / 'c.db'}", jwt_secret=SECRET)
    c = TestClient(app_)
    assert c.get("/").status_code == 404
    assert c.get("/api/health").status_code == 200


def test_resolve_reports_disabled_not_missing(monkeypatch) -> None:
    """关闭态的原因文案要与"没构建"区分开，否则排障时被误导。"""
    from skillos.api.web import resolve_web_dist

    monkeypatch.setenv("SKILLOS_WEB_DIST", "off")
    dist, reason = resolve_web_dist()
    assert dist is None
    assert "关闭" in reason
