"""日志面板接口的响应契约（FR-1.1.12）。

2026-10-26 实测两个坑，都在 `/api/logs` 这一条路由上：

1. **成功也没有信封**：路由直接 `return data`，而前端 `api.get` 只认
   `{ok, data, error}` → `body.data` 永远 undefined，日志面板事实上取不到数据；
2. **失败只有 HTTP 码**：`{"detail": "TIMEOUT"}` → 界面显示 "502 Bad Gateway"，
   分不清「daemon 没回包」和「daemon 报错」。

现在统一走 `_daemon_proxy.call_daemon`，这里固定：
成功给 `{ok:true,data}`；超时/离线/报错都给 `{ok:false,error:{code,message}}`，
code 保留原始语义（TIMEOUT / DAEMON_OFFLINE / daemon 自己的错误码）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillos.api.main import create_app


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(
        db_url=f"sqlite:///{tmp_path / 'cloud.db'}", jwt_secret="test-secret",
    )
    with TestClient(app) as c:
        yield c


def _token(client: TestClient) -> str:
    client.post("/api/auth/register", json={
        "email": "u@x.com", "password": "password123",
    })
    return client.post("/api/auth/login", json={
        "email": "u@x.com", "password": "password123",
    }).json()["data"]["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestLogsRoute:
    def test_success_uses_envelope(self, client: TestClient, monkeypatch) -> None:
        """成功必须带信封——否则前端 api.get 解析不出 data（曾整块面板不可用）。"""
        token = _token(client)

        async def _ok(user_id: str, op: str, params: dict, timeout: float | None = None):
            return {"ok": True, "data": {"events": [{"level": "INFO"}], "skipped": 2}}

        monkeypatch.setattr(client.app.state.hub, "submit_task", _ok)
        r = client.get("/api/logs?mode=daemon", headers=_auth(token))
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["error"] is None
        assert body["data"]["skipped"] == 2
        assert body["data"]["events"][0]["level"] == "INFO"

    def test_timeout_keeps_readable_message(self, client: TestClient, monkeypatch) -> None:
        """超时：code=TIMEOUT + 可读 message（面板据此区别于离线）。"""
        token = _token(client)

        async def _slow(user_id: str, op: str, params: dict, timeout: float | None = None):
            return {"ok": False,
                    "error": {"code": "TIMEOUT", "message": "daemon 未在 15s 内回包"}}

        monkeypatch.setattr(client.app.state.hub, "submit_task", _slow)
        r = client.get("/api/logs?mode=daemon&tail=5&level=INFO", headers=_auth(token))
        body = r.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "TIMEOUT"
        assert "未在 15s 内回包" in body["error"]["message"]

    def test_daemon_error_keeps_code(self, client: TestClient, monkeypatch) -> None:
        """daemon 侧报错：错误码原样透出，不被吞成 INTERNAL。"""
        token = _token(client)

        async def _boom(user_id: str, op: str, params: dict, timeout: float | None = None):
            return {"ok": False, "error": {"code": "IO", "message": "读日志失败"}}

        monkeypatch.setattr(client.app.state.hub, "submit_task", _boom)
        body = client.get("/api/logs?mode=daemon", headers=_auth(token)).json()
        assert body["error"]["code"] == "IO"

    def test_offline_code_is_daemon_offline(self, client: TestClient) -> None:
        """daemon 不在线：信封里的 code 仍是 DAEMON_OFFLINE（前端按码分支）。"""
        token = _token(client)
        body = client.get("/api/logs?mode=daemon", headers=_auth(token)).json()
        assert body["ok"] is False
        assert body["error"]["code"] == "DAEMON_OFFLINE"

    def test_unknown_mode_is_422(self, client: TestClient) -> None:
        token = _token(client)
        r = client.get("/api/logs?mode=nope", headers=_auth(token))
        assert r.status_code == 422
