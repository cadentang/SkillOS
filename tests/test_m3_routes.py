"""M3 路由契约（N-03 / N-08 / E-04 / N-07）。

规则面板与相似对**全部是本地数据**（`.skillos/rules/` + `skill_similarity` 表），
云端只代理。这条路径的失败方式很脏：

- 路由漏注册 → 前端 404，但 pytest 服务层全绿（`test_settings_routes.py` 里记过这个坑）；
- daemon 离线时返回空列表 → 界面显示"没有重复 skill"，与"读不到"混为一谈；
- op 没进 `WS_TASK_OPS` → 云端下发被拒（OUT-14）。

三个断言分别守住这三条。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skillos.api.main import create_app
from skillos.core.constants import WS_TASK_OPS

M3_ENDPOINTS = [
    ("GET", "/api/rules"),
    ("GET", "/api/similarity"),
    ("POST", "/api/similarity/run"),
]


@pytest.fixture()
def client(tmp_path):
    """无 daemon 连接的 TestClient：端点存在性 + 离线错误码。"""
    app = create_app(
        db_url=f"sqlite:///{tmp_path / 'cloud.db'}", jwt_secret="test-secret",
    )
    with TestClient(app) as c:
        yield c


def _token(client: TestClient) -> str:
    client.post("/api/auth/register", json={
        "email": "u@example.com", "password": "password123",
    })
    return client.post("/api/auth/login", json={
        "email": "u@example.com", "password": "password123",
    }).json()["data"]["token"]


class TestM3RoutesRegistered:
    @pytest.mark.parametrize("method,path", M3_ENDPOINTS)
    def test_endpoint_registered_not_404(self, client, method, path):
        """401（auth 失败）而非 404（路由未注册）。"""
        assert client.request(method, path).status_code == 401, (
            f"{method} {path} 应 401（路由存在）而不是 404（未注册）"
        )

    @pytest.mark.parametrize("method,path", M3_ENDPOINTS)
    def test_offline_returns_explicit_code(self, client, method, path):
        """daemon 离线 → DAEMON_OFFLINE，不伪装成空数据。"""
        token = _token(client)
        body = client.request(
            method, path, headers={"Authorization": f"Bearer {token}"},
        ).json()
        assert body["ok"] is False
        assert body["error"]["code"] == "DAEMON_OFFLINE"

    def test_m3_ops_in_whitelist(self):
        """OUT-14：三个具名 op 必须进白名单，否则云端下发会 500。"""
        required = {"rules_list", "similarity_list", "similarity_run"}
        assert required <= WS_TASK_OPS, sorted(required - WS_TASK_OPS)


class _FakeHub:
    """在线 daemon 的替身：只回放数据，不碰 WS / 磁盘。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    async def submit_task(self, user_id: str, op: str, params: dict) -> dict:
        self.calls.append((op, params))
        return self.payload


class TestM3RoutesOnline:
    def test_rules_panel_passthrough(self, client):
        """在线时原样透出 daemon 的四块数据（rules / invalid / ignores / hash）。"""
        hub = _FakeHub({"ok": True, "data": {
            "ruleset_hash": "abc123",
            "files": ["security.yaml"],
            "rules": [{
                "rule_id": "custom.no-todo", "level": "warn", "target": "file_content",
                "pattern": "TODO", "flags": [], "scope_paths": [], "scope_exclude": [],
                "message": "别留 TODO", "category": None, "source": "security.yaml",
            }],
            "invalid": [{"rule_id": "bad", "reason": "缺少前缀", "source": "x.yaml"}],
            "ignores": [{"rule_id": "security.github-token", "paths": ["assets/*"],
                         "reason": "测试数据"}],
        }})
        client.app.state.hub = hub
        token = _token(client)
        body = client.get(
            "/api/rules", headers={"Authorization": f"Bearer {token}"},
        ).json()

        assert body["ok"] is True
        data = body["data"]
        assert data["ruleset_hash"] == "abc123"
        assert data["invalid"][0]["reason"] == "缺少前缀"
        assert data["ignores"][0]["reason"] == "测试数据"
        assert hub.calls == [("rules_list", {})]

    def test_similarity_list_passthrough(self, client):
        hub = _FakeHub({"ok": True, "data": {"items": [{
            "a": "one", "b": "two", "score": 0.93, "method": "tfidf",
            "merge_candidate": True,
            "suggestion": {"keep": "one", "drop": "two", "reason": "创建时间更早"},
        }]}})
        client.app.state.hub = hub
        token = _token(client)
        body = client.get(
            "/api/similarity", headers={"Authorization": f"Bearer {token}"},
        ).json()

        assert body["data"]["items"][0]["suggestion"]["keep"] == "one"
        assert hub.calls == [("similarity_list", {})]

    def test_similarity_run_passthrough(self, client):
        hub = _FakeHub({"ok": True, "data": {
            "pairs": 1, "added": 0, "skills": 2, "threshold": 0.75,
        }})
        client.app.state.hub = hub
        token = _token(client)
        body = client.post(
            "/api/similarity/run", headers={"Authorization": f"Bearer {token}"},
        ).json()

        assert body["data"]["added"] == 0
        assert hub.calls == [("similarity_run", {})]

    def test_daemon_rejection_is_not_masked(self, client):
        """daemon 拒答（例如规则文件损坏）→ 原样透出错误码，不降级成空列表。"""
        hub = _FakeHub({"ok": False, "error": {"code": "INVALID_INPUT", "message": "规则文件损坏"}})
        client.app.state.hub = hub
        token = _token(client)
        body = client.get(
            "/api/rules", headers={"Authorization": f"Bearer {token}"},
        ).json()

        assert body["ok"] is False
        assert body["error"]["code"] == "INVALID_INPUT"
