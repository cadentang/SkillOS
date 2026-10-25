"""daemon 主动上报用量（N-01.4 每日 / N-01.9 重连补报）—— 推送路径的验收。

三条硬约束（每条都对应一种"看起来成功、其实没生效"）：
1. **开关关闭零上报**：`push_usage_report` 返回 0 且**一条都不发**（不是发空数组）；
2. **云端不采信自报归属**：`device_id` / `user_id` 一律取认证连接，报文里的值无效；
3. **重发不翻倍**：覆盖式合并 ⇒ 同一份 30 天全量重报多少次结果都一样。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillos.api.main import create_app
from skillos.core.algorithm.usage import sanitize_report_rows
from skillos.core.constants import USAGE_REPORT_FIELDS, USAGE_REPORT_MAX_ROWS
from skillos.daemon.main import push_usage_report
from skillos.services.engine import EnvPaths, LocalEngine

_SKILL_MD = (
    b"---\nname: demo\nversion: 1.0.0\n"
    b"description: A demo skill used by usage report push tests only.\n---\nbody\n"
)


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        db_url=f"sqlite:///{tmp_path / 'cloud.db'}", jwt_secret="test-secret",
    )
    with TestClient(app) as c:
        yield c


def _jwt(client: TestClient, email: str = "u@example.com") -> str:
    client.post("/api/auth/register", json={
        "email": email, "password": "password123",
    })
    return client.post("/api/auth/login", json={
        "email": email, "password": "password123",
    }).json()["data"]["token"]


def _agent_token(client: TestClient, email: str = "u@example.com",
                 agent_id: str = "test-agent") -> str:
    """签发一枚设备 token（明文只在这里出现一次，库里只存哈希）。"""
    from skillos.api.security import new_agent_token
    from skillos.store.cloud_repo import CloudRepo

    repo = CloudRepo(client.app.state.session_factory())
    user = repo.get_user_by_email(email)
    tid, plain, thash = new_agent_token()
    repo.issue_agent_token(user.id, agent_id, tid, thash, platform="windows")
    return plain


def _row(**over) -> dict:
    base = {
        "device_id": "local",           # 设备自报值 —— 云端必须忽略
        "skill_id": "demo",
        "date": datetime.now(UTC).date().isoformat(),
        "via": "mcp",
        "count": 3,
        "tokens_in": 10,
        "tokens_out": 20,
        "duration_ms_p50": 5,
        "duration_ms_p95": 9,
        "by_op": '{"get_skill": 3}',
    }
    base.update(over)
    return base


class _FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


def _engine(tmp_path: Path) -> LocalEngine:
    home = tmp_path / "home"
    d = home / ".claude" / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(_SKILL_MD)
    engine = LocalEngine(
        EnvPaths(repo_root=tmp_path / "repo", db_path=tmp_path / "state" / "s.db"),
        home=home,
    )
    engine.init_env()
    engine.import_skills()
    return engine


class TestDeviceSidePush:
    def test_disabled_sends_nothing(self, tmp_path: Path) -> None:
        """N-01.5：开关默认 off ⇒ 返回 0 且**一帧都不发**（零上报可断言）。"""
        engine = _engine(tmp_path)
        engine.usage.record("demo", "get_skill", via="mcp")
        engine.usage.aggregate()

        ws = _FakeWs()
        sent = asyncio.run(push_usage_report(ws, engine))
        assert sent == 0
        assert ws.sent == []

    def test_enabled_sends_only_aggregate_fields(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.settings.set("usage_report_enabled", True)
        engine.usage.record("demo", "get_skill", via="mcp", digest="deadbeef")
        engine.usage.aggregate()

        ws = _FakeWs()
        sent = asyncio.run(push_usage_report(ws, engine))
        assert sent > 0
        assert len(ws.sent) == 1
        msg = ws.sent[0]
        assert msg["type"] == "event" and msg["event"] == "usage_report"
        rows = msg["payload"]["rows"]
        assert rows
        for row in rows:
            assert set(row) <= USAGE_REPORT_FIELDS
            assert "deadbeef" not in str(row)        # 参数摘要不上云
            assert str(tmp_path) not in str(row)     # 本机路径不上云


class TestSanitizeReportRows:
    """云端第二道防线：不信自报、只留白名单字段。"""

    def test_stamps_device_id_and_drops_unknown_fields(self) -> None:
        rows, skipped = sanitize_report_rows(
            [{**_row(), "secret_path": "C:/private", "user_id": "someone-else"}],
            device_id="auth-agent",
        )
        assert skipped == 0
        assert len(rows) == 1
        assert rows[0]["device_id"] == "auth-agent"
        assert set(rows[0]) == USAGE_REPORT_FIELDS

    def test_malformed_rows_are_counted_not_swallowed(self) -> None:
        rows, skipped = sanitize_report_rows(
            [
                _row(),
                {"skill_id": "", "via": "mcp", "date": "2026-10-24", "count": 1},
                _row(count=-5),
                _row(date="not-a-date"),
                "not-a-dict",
            ],
            device_id="auth-agent",
        )
        assert len(rows) == 1
        assert skipped == 4

    def test_non_array_is_protocol_error(self) -> None:
        """整批形态错误要报错 —— 静默清洗会把"报文变形"伪装成"今天没数据"。"""
        with pytest.raises(ValueError):
            sanitize_report_rows({"rows": []}, device_id="a")
        with pytest.raises(ValueError):
            sanitize_report_rows([_row()] * (USAGE_REPORT_MAX_ROWS + 1), device_id="a")

    def test_none_is_empty(self) -> None:
        assert sanitize_report_rows(None, device_id="a") == ([], 0)


class TestCloudIngest:
    """WS 上行 → 合并落库 → 汇总可见（N-01.4/9）。"""

    def _push(self, client: TestClient, token: str, rows: list[dict],
              agent_id: str = "test-agent", **extra) -> dict:
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({"type": "hello", "agent_id": agent_id, "token": token})
            ws.receive_json()  # welcome
            ws.send_json({"type": "event", "event": "usage_report",
                          "payload": {"rows": rows}, **extra})
            for _ in range(5):
                msg = ws.receive_json()
                if msg.get("type") == "event_ack":
                    return msg
                if msg.get("type") == "task":
                    # 重连自愈（FR-1.0.34）会插一条 custom Runtime 对齐任务
                    ws.send_json({"type": "result", "task_id": msg["task_id"],
                                  "request_id": msg["request_id"],
                                  "ok": True, "data": {}})
            raise AssertionError("5 条消息内未收到 event_ack")

    def _summary(self, client: TestClient, jwt: str) -> list[dict]:
        return client.get("/api/daemon/usage/summary?days=30",
                          headers={"Authorization": f"Bearer {jwt}"}
                          ).json()["data"]["items"]

    def test_report_lands_and_is_idempotent(self, client) -> None:
        jwt = _jwt(client)
        token = _agent_token(client)      # 同一台设备（同一 token）重复上报
        ack = self._push(client, token, [_row(count=3)])
        assert ack["ok"] is True and ack["merged"] == 1

        # N-01.9：同一份数据重发（重连补报）不得翻倍
        self._push(client, token, [_row(count=3)])
        self._push(client, token, [_row(count=3)])

        items = self._summary(client, jwt)
        assert [(i["skill_id"], i["count"], i["device_count"]) for i in items] == [
            ("demo", 3, 1),
        ]

    def test_self_declared_user_id_is_ignored(self, client) -> None:
        """归属取认证连接 —— 否则任何一台 daemon 都能往别人账号里写数据。"""
        jwt = _jwt(client)
        ack = self._push(client, _agent_token(client), [_row()],
                         user_id="someone-else")
        assert ack["ok"] is True
        # 数据落在**认证用户**名下（若采信报文 user_id，这里会是空列表）
        assert len(self._summary(client, jwt)) == 1

    def test_device_bucket_is_token_not_agent_id(self, client) -> None:
        """分桶键 = token_id：同名机器（同 agent_id）仍必须是两台设备。

        agent_id 是 `platform-hostname`，同账号两台同名机器会撞桶 —— 用 token 才不撞。
        """
        jwt = _jwt(client)
        self._push(client, _agent_token(client), [_row()], agent_id="same-host")
        self._push(client, _agent_token(client), [_row(count=4)], agent_id="same-host")

        items = self._summary(client, jwt)
        assert [(i["count"], i["device_count"]) for i in items] == [(7, 2)]

    def test_summary_scoped_to_own_devices(self, client) -> None:
        """别人的设备桶不得进我的汇总（`skill_usage_daily` 没有 user_id 列）。"""
        jwt_a = _jwt(client, "a@example.com")
        jwt_b = _jwt(client, "b@example.com")
        self._push(client, _agent_token(client, "a@example.com"), [_row(count=2)])
        self._push(client, _agent_token(client, "b@example.com"),
                   [_row(count=99)], agent_id="b-host")

        assert [i["count"] for i in self._summary(client, jwt_a)] == [2]
        assert [i["count"] for i in self._summary(client, jwt_b)] == [99]

    def test_unknown_event_rejected(self, client) -> None:
        _jwt(client)  # 先建用户（连接鉴权依赖库里的 agent token）
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({"type": "hello", "agent_id": "test-agent",
                          "token": _agent_token(client)})
            ws.receive_json()
            ws.send_json({"type": "event", "event": "rm_rf", "payload": {}})
            for _ in range(5):
                msg = ws.receive_json()
                if msg.get("type") == "error":
                    assert msg["error"]["code"] == "UNKNOWN_EVENT"
                    return
                if msg.get("type") == "task":
                    ws.send_json({"type": "result", "task_id": msg["task_id"],
                                  "request_id": msg["request_id"],
                                  "ok": True, "data": {}})
            raise AssertionError("未收到 UNKNOWN_EVENT")
