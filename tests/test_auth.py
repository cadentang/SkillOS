"""认证与配对安全测试（FR-3.1 / FR-1.2.3 / FR-3.15 / FR-1.2.8）。

坑（07 配对限流）：6 位码 = 10⁶ 空间、10 分钟窗口，不限流理论上可枚举。
连续 5 次失败必须锁 60s —— 变异验证：把阈值改大/删除锁定逻辑，测试必红。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skillos.api.main import create_app
from skillos.api.security import hash_token, verify_jwt
from skillos.store.cloud_repo import aggregate_skill_status


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        db_url=f"sqlite:///{tmp_path / 'cloud.db'}",
        jwt_secret="test-secret",
    )
    with TestClient(app) as c:
        yield c


def _register_and_login(client: TestClient) -> str:
    r = client.post("/api/auth/register", json={
        "email": "u@example.com", "password": "password123", "name": "u",
    })
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", json={
        "email": "u@example.com", "password": "password123",
    })
    assert r.status_code == 200
    return r.json()["data"]["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAuth:
    def test_register_login_jwt_roundtrip(self, client):
        token = _register_and_login(client)
        assert verify_jwt(token, "test-secret") is not None
        r = client.get("/api/auth/me", headers=_auth(token))
        assert r.status_code == 200
        assert r.json()["data"]["email"] == "u@example.com"

    def test_duplicate_email_rejected(self, client):
        _register_and_login(client)
        r = client.post("/api/auth/register", json={
            "email": "u@example.com", "password": "password123",
        })
        assert r.status_code == 409

    def test_wrong_password_401(self, client):
        _register_and_login(client)
        r = client.post("/api/auth/login", json={
            "email": "u@example.com", "password": "wrong-password",
        })
        assert r.status_code == 401

    def test_no_token_401(self, client):
        assert client.get("/api/auth/me").status_code == 401
        assert client.get("/api/sync/status").status_code == 401


class TestPairingRateLimit:
    """变异验证：删除 record_scope_failure 的锁定分支 → 最后一个用例必红。"""

    def test_five_failures_lock_ip_scope(self, client):
        """同一 ip 连续 5 次错码 → 锁定，第 6 次即使码对也拒绝（FR-3.15）。"""
        token = _register_and_login(client)
        # 正确码留到锁定后验证「锁定期间一律拒绝」
        good = client.post("/api/pairing/code", headers=_auth(token)).json()["data"]["code"]

        for _ in range(5):
            with client.websocket_connect("/ws/agent") as ws:
                ws.send_json({
                    "type": "hello", "agent_id": "a1",
                    "pair_code": "000000", "platform": "windows",
                })
                msg = ws.receive_json()
                assert msg["type"] == "error"
                assert msg["error"]["code"] in ("PAIRING_FAILED",)

        # 已锁：正确码也被拒（锁定期间一律拒绝）
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({
                "type": "hello", "agent_id": "a1",
                "pair_code": good, "platform": "windows",
            })
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert msg["error"]["code"] == "RATE_LIMITED"

    def test_valid_code_pairs_and_token_works(self, client):
        token = _register_and_login(client)
        code = client.post("/api/pairing/code", headers=_auth(token)).json()["data"]["code"]
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({
                "type": "hello", "agent_id": "dev-01",
                "pair_code": code, "platform": "windows",
            })
            ack = ws.receive_json()
            assert ack["type"] == "paired"
            plain_token = ack["token"]
            assert ack["user_id"]

        # 签发的 token 可二次连接；库中只存哈希（NFR-2）
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({
                "type": "hello", "agent_id": "dev-01", "token": plain_token,
            })
            assert ws.receive_json()["type"] == "welcome"

        devices = client.get("/api/devices", headers=_auth(token)).json()["data"]
        assert len(devices) == 1
        assert devices[0]["agent_id"] == "dev-01"
        # token 明文与库内哈希不同
        assert devices[0]["id"] != plain_token

    def test_pairing_code_single_use(self, client):
        token = _register_and_login(client)
        code = client.post("/api/pairing/code", headers=_auth(token)).json()["data"]["code"]
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({"type": "hello", "agent_id": "d", "pair_code": code})
            assert ws.receive_json()["type"] == "paired"
        # 第二次用同一码 → 拒绝
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({"type": "hello", "agent_id": "d2", "pair_code": code})
            msg = ws.receive_json()
            assert msg["type"] == "error"

    def test_revoke_token_disconnects(self, client):
        token = _register_and_login(client)
        code = client.post("/api/pairing/code", headers=_auth(token)).json()["data"]["code"]
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({"type": "hello", "agent_id": "dev-x", "pair_code": code})
            plain = ws.receive_json()["token"]

        devices = client.get("/api/devices", headers=_auth(token)).json()["data"]
        r = client.delete(f"/api/devices/{devices[0]['id']}", headers=_auth(token))
        assert r.status_code == 200

        # 撤销后 token 不再可用
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({"type": "hello", "agent_id": "dev-x", "token": plain})
            assert ws.receive_json()["type"] == "error"


class TestTokenHash:
    def test_agent_token_stored_as_hash(self):
        from skillos.api.security import new_agent_token

        _id, plain, h = new_agent_token()
        assert plain != h
        assert hash_token(plain) == h


class TestAggregate:
    def test_worst_state_wins(self):
        assert aggregate_skill_status(["synced", "outdated"]) == "OUTDATED"
        assert aggregate_skill_status(["synced", "conflict", "missing"]) == "CONFLICT"
        assert aggregate_skill_status(["synced", "synced"]) == "SYNCED"

    def test_partial_state(self):
        """07 N11：部分分发 ≠ 未分发 ≠ 已关闭，必须显示 PARTIAL。"""
        assert aggregate_skill_status(["synced", "unmanaged"]) == "PARTIAL"
        assert aggregate_skill_status(["synced", "orphaned"]) == "PARTIAL"

    def test_single_unmanaged_stays_unmanaged(self):
        """无任何 synced 副本时不得标 PARTIAL（PARTIAL 前提是部分已分发）。"""
        assert aggregate_skill_status(["unmanaged"]) == "UNMANAGED"
        assert aggregate_skill_status(["orphaned", "unmanaged"]) == "ORPHANED"

    def test_actionable_wins_over_partial(self):
        """含行动类状态（OUTDATED/CONFLICT…）时取最严重，不降级为 PARTIAL。"""
        assert aggregate_skill_status(["synced", "outdated"]) == "OUTDATED"
        assert aggregate_skill_status(["synced", "unmanaged", "conflict"]) == "CONFLICT"

    def test_empty_is_missing(self):
        assert aggregate_skill_status([]) == "MISSING"
