"""Settings 页 HTTP 路由冒烟测试（FR-2.4.10 / T26 / FR-1.0.29）。

覆盖 P3 升级"前端调端点 → 后端路由已注册"的全集。

之前 c8af6f3 之后多个端点（P3 Settings 区块）没在 HTTP 层被任何测试直接
调过 —— 导致 `/api/protocol/status` 漏注册 404 而 pytest 375 全绿。

守门：
- 端点存在（401 而非 404 —— 区分"路由未注册" vs "auth 失败"）
- daemon 离线 → 503 DAEMON_OFFLINE（显式错误码，用户能区分原因）
- WS_TASK_OPS 白名单同步（OUT-14 红线守）

不做什么（D8 / OUT-14 红线保持）：
- 不起真 daemon 子进程（TestClient + 无 hub 注入）
- 不连真 WS
- 不写磁盘
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skillos.api.main import create_app
from skillos.core.constants import WS_TASK_OPS


@pytest.fixture()
def client(tmp_path):
    """无 daemon 连接的 TestClient —— 用来测端点存在 + 离线 503."""
    app = create_app(
        db_url=f"sqlite:///{tmp_path / 'cloud.db'}", jwt_secret="test-secret",
    )
    with TestClient(app) as c:
        yield c


def _token(client: TestClient) -> str:
    """最小可用 token: register + login."""
    client.post("/api/auth/register", json={
        "email": "u@example.com", "password": "password123",
    })
    return client.post("/api/auth/login", json={
        "email": "u@example.com", "password": "password123",
    }).json()["data"]["token"]


class TestSettingsRoutesRegistered:
    """P3 Settings 区块全部 HTTP 端点路由必须注册（不 404）."""

    ENDPOINTS_REGISTERED = [
        # Settings · Runtime 区块（FR-2.4.10 / T24~T26）
        ("GET", "/api/runtimes/discovered"),
        # Settings · 守护区块
        ("GET", "/api/daemon/status"),
        # Settings · 协议区块（FR-1.0.29 / T18）—— 这次 404 的就是它
        ("GET", "/api/protocol/status"),
    ]

    @pytest.mark.parametrize("method,path", ENDPOINTS_REGISTERED)
    def test_endpoint_registered_not_404(self, client, method, path):
        """端点必须 401（auth 失败）而非 404（路由未注册）.

        区分: 401 = 路由存在, auth 失败; 404 = 路由未注册, 路由表里没这条.
        P3 升级时 protocol router 漏注册 → 404 持续被前端撞, 走查根因.
        """
        r = client.request(method, path)
        assert r.status_code != 404, (
            f"{method} {path} 路由未注册, 应当 401 (auth 失败) 而非 404"
        )
        assert r.status_code == 401, (
            f"{method} {path} 期望 401, 实际 {r.status_code}: {r.text[:200]}"
        )


class TestProtocolStatusOffline:
    """GET /api/protocol/status 在 daemon 离线时必须返回结构化 envelope.

    设计决策（**不抛 503**，仿 `daemon.py` 风格）:
    - 离线时返回 `{ok: True, data: {ok: False, reason: "daemon offline", ...}}`
    - 抛 503 会让 `client.ts:request` 抛 ApiError → useQuery data=undefined →
      前端 "data is undefined" —— 上一版就撞这个坑.
    """

    def test_protocol_status_daemon_offline_returns_envelope(self, client):
        """无 daemon → 200 + envelope {ok: True, data: {ok: False, reason: 'daemon offline'}}.

        区分"协议没握手"与"代码抛异常": 前端可以分别提示.
        """
        token = _token(client)
        r = client.get(
            "/api/protocol/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, (
            f"daemon 离线应 200 (envelope 风格), 实际 {r.status_code}: {r.text[:200]}"
        )
        body = r.json()
        assert body["ok"] is True
        assert body["data"]["ok"] is False
        assert body["data"]["reason"] == "daemon offline"
        # 8 字段占位（current_supported 等设为 None, 前端 interface 允许）
        assert body["data"]["current_supported"] is None
        assert body["data"]["sticky_version"] is None
        assert body["data"]["read_only"] is True

    def test_protocol_status_op_in_whitelist(self):
        """OUT-14 红线: protocol_status 必须在 WS_TASK_OPS 白名单里.

        否则云端路由 500 报"非白名单操作" —— 这是构造期 + 运行期双保险.
        """
        assert "protocol_status" in WS_TASK_OPS, (
            "protocol_status 不在 WS_TASK_OPS → 云端路由会拒(500)"
        )
