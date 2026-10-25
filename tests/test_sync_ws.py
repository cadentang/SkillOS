"""WS 任务闭环测试（FR-1.2.4 白名单 / 07 N12 request_id 守卫 / FR-2.1.9 聚合上报）。

用真实 LocalEngine + 临时 home：在磁盘上真实制造状态（03 §8.2 禁止伪造中间量）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillos.api.main import create_app
from skillos.daemon.main import TaskExecutor
from skillos.services.engine import LocalEngine, default_env

_SKILL_MD = (
    b"---\nname: demo\nversion: 1.2.0\n"
    b"description: a demo skill doing useful work\n---\nbody\n"
)


@pytest.fixture()
def engine_home(tmp_path: Path):
    home = tmp_path / "home"
    d = home / ".claude" / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(_SKILL_MD)
    engine = LocalEngine(default_env(home), home=home)
    engine.init_env()
    engine.import_skills()
    return engine, home


@pytest.fixture()
def client(tmp_path):
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


def _pair(client: TestClient, token: str) -> None:
    code = client.post("/api/pairing/code",
                       headers={"Authorization": f"Bearer {token}"}).json()["data"]["code"]
    with client.websocket_connect("/ws/agent") as ws:
        ws.send_json({"type": "hello", "agent_id": "test-agent",
                      "pair_code": code, "platform": "windows"})
        assert ws.receive_json()["type"] == "paired"


class TestTaskExecutor:
    def test_whitelist_rejects_unknown_op(self, engine_home):
        engine, _ = engine_home
        ex = TaskExecutor(engine)
        r = ex.execute("rm_rf", {})
        assert not r["ok"]
        assert r["error"]["code"] == "INVALID_INPUT"

    def test_status_reports_matrix(self, engine_home):
        engine, _ = engine_home
        r = TaskExecutor(engine).execute("status", {})
        assert r["ok"]
        cells = r["data"]["matrix"]
        assert cells and all("slug" in c and "runtime_key" in c and "state" in c
                             for c in cells)

    def test_push_via_executor(self, engine_home):
        engine, _ = engine_home
        r = TaskExecutor(engine).execute("push", {"skill_ids": ["demo"]})
        assert r["ok"], r
        assert all(item["ok"] for item in r["data"]["results"])

    def test_p3_ops_not_silently_ok(self, engine_home):
        """pull 已在 P5 接线（FR-2.2.6）：空参必须结构化 INVALID_INPUT，不得静默成功。"""
        engine, _ = engine_home
        r = TaskExecutor(engine).execute("pull", {})
        assert not r["ok"]
        assert r["error"]["code"] == "INVALID_INPUT"

    def test_file_write_stale_rejected(self, engine_home):
        """FR-3.13：base_hash 不匹配必须拒绝（STALE），不允许覆盖。"""
        engine, _ = engine_home
        r = TaskExecutor(engine).execute("file_write", {
            "slug": "demo", "path": "SKILL.md",
            "content": "---\nname: demo\nversion: 1.2.0\ndescription: changed\n---\n",
            "base_hash": "0" * 64, "message": "stale write",
        })
        assert not r["ok"]
        assert r["error"]["code"] == "STALE"

    def test_file_write_then_gate_findings(self, engine_home):
        """合法写 → commit + 返回逐条门禁 findings（保存不拦截，只提示）。"""
        engine, _ = engine_home
        import hashlib

        tree = TaskExecutor(engine).execute("file_tree", {"slug": "demo"})
        assert tree["ok"] and any(f["path"] == "SKILL.md" for f in tree["data"]["tree"])
        base = next(f["hash"] for f in tree["data"]["tree"] if f["path"] == "SKILL.md")
        content = engine.paths.repo_root.joinpath("skills/demo/SKILL.md").read_bytes()
        r = TaskExecutor(engine).execute("file_write", {
            "slug": "demo", "path": "SKILL.md",
            "content": content.decode(),
            "base_hash": base, "message": "roundtrip",
        })
        assert r["ok"]
        assert r["data"]["hash"] == hashlib.sha256(content).hexdigest()
        assert isinstance(r["data"]["gate"], list)

        # 门禁逐条（FR-1.0.11）
        c = TaskExecutor(engine).execute("check", {"slug": "demo"})
        assert c["ok"] and "findings" in c["data"]
        # 折旧（FR-1.0.23）
        d = TaskExecutor(engine).execute("decay", {"slug": "demo"})
        assert d["ok"]
        assert d["data"]["staleness_days"] is not None
        assert d["data"]["dependency_broken"] == []

    def test_batch_single_failure_does_not_block_rest(self, engine_home):
        """FR-2.1.4：批量中单个 Skill 失败不阻塞其余 —— 好的照常分发。"""
        engine, _ = engine_home
        r = TaskExecutor(engine).execute("push", {"skill_ids": ["demo", "ghost"]})
        assert r["ok"] is False
        by_skill = {i["skill"]: i for i in r["data"]["results"] if i["skill"] == "demo"}
        assert by_skill and all(i["ok"] for i in by_skill.values())
        ghost = [i for i in r["data"]["results"] if i["skill"] == "ghost"]
        assert ghost and not ghost[0]["ok"]
        assert r["error"]["code"] == "INVALID_INPUT"

    def test_removals_error_code_priority(self):
        """FR-3.19：删除待确认（REMOVALS_REQUIRE_CONFIRM）优先于其它失败码。"""
        results = [
            {"skill": "a", "runtime": "claude", "ok": False,
             "error": {"code": "TARGET_CONFLICT", "message": ""}},
        ]
        assert TaskExecutor._batch_error_code(results, ["x/old.md"]) == "REMOVALS_REQUIRE_CONFIRM"
        assert TaskExecutor._batch_error_code(results, []) == "TARGET_CONFLICT"

    def test_gate_summary_counts(self, engine_home):
        """ManagerPanel 门禁计数：合法 skill → error=0；由 engine.gate_of 统一判定。"""
        engine, _ = engine_home
        r = TaskExecutor(engine).execute("gate_summary", {})
        assert r["ok"]
        assert "demo" in r["data"]["summary"]
        assert r["data"]["summary"]["demo"]["errors"] == 0


class TestRequestDedup:
    def test_dedup_window(self):
        from skillos.daemon.main import RequestDedup

        d = RequestDedup(size=3)
        for i in range(3):
            d.mark(f"r{i}", f"t{i}")
        assert d.seen("r0")
        d.mark("r3", "t3")  # 挤出最旧的 r0
        assert not d.seen("r0")
        assert d.seen("r3")


class TestWsTaskRoundtrip:
    def test_push_down_to_daemon_and_result_back(self, client, engine_home):
        """REST push → 云端下发 task → 模拟 daemon 执行 → result 回传 REST 响应。"""
        import threading

        engine, _ = engine_home
        token = _token(client)
        _pair(client, token)

        def daemon_side(ws, stop: threading.Event):
            try:
                while not stop.is_set():
                    msg = ws.receive_json()
                    if msg["type"] != "task":
                        continue
                    assert msg["request_id"], "任务必须带 request_id（07 N12）"
                    result = TaskExecutor(engine).execute(msg["op"], msg["params"])
                    ws.send_json({"type": "result", "task_id": msg["task_id"],
                                  "request_id": msg["request_id"], **result})
            except Exception:  # noqa: BLE001 —— ws 关闭后退出属正常收尾
                pass

        stop = threading.Event()
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({"type": "hello", "agent_id": "test-agent",
                          "token": _agent_token(client, token)})
            ws.receive_json()  # welcome
            t = threading.Thread(target=daemon_side, args=(ws, stop), daemon=True)
            t.start()
            r = client.post("/api/sync/push",
                            headers={"Authorization": f"Bearer {token}"},
                            json={"skill_ids": ["demo"]})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"], body
            assert all(x["ok"] for x in body["data"]["results"])
            stop.set()

    def test_duplicate_result_ignored(self, client, engine_home):
        """同一 request_id 的 result 只消费一次（07 N12 守卫①）。

        注意：必须单事件循环 —— submit_task 的 future 属于当前循环，
        若 result 在另一循环上 set_result（TestClient 的 app loop 与手动
        new_event_loop 混用），future 的唤醒回调会被调度到错误的循环上，
        await 永远不返回（死锁）。故此处用 fake ws 在同一循环内驱动。
        """
        import asyncio

        from skillos.api.ws_hub import AgentConn, WsHub

        class FakeWs:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            async def send_json(self, msg: dict) -> None:
                self.sent.append(msg)

            async def close(self, **_kw) -> None:
                pass

        async def scenario() -> None:
            hub = WsHub()
            conn = AgentConn(ws=FakeWs(), agent_id="a1", user_id="u1", token_id="t1")
            await hub.register_agent(conn)
            # 提交任务后立即投递两次同一 request_id 的 result
            submit = asyncio.create_task(hub.submit_task("u1", "status", {}, timeout=5.0))
            await asyncio.sleep(0)  # 让 submit_task 完成下发
            task_msg = conn.ws.sent[0]
            assert task_msg["type"] == "task" and task_msg["request_id"]
            rid = task_msg["request_id"]
            await hub.handle_result(rid, {"ok": True, "data": {"matrix": []}})
            await hub.handle_result(rid, {"ok": False, "error": {"code": "IO",
                                                                 "message": "dup"}})
            result = await submit
            assert result["ok"] is True  # 第一次生效，重复 result 未覆盖

        asyncio.run(scenario())

    def test_task_done_broadcast_carries_op(self):
        """task_done 广播必须带 op 字段（04 §4.2，自激循环修复的契约侧）。

        前端按 op 区分读写：只对写集失效 drift/gate。若 op 缺失，前端
        无法区分「读任务完成」与「数据变更」，读任务重取自身又产生
        task_done，回到 gate-summary/status 请求风暴。
        """
        import asyncio

        from skillos.api.ws_hub import AgentConn, WsHub

        class FakeWs:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            async def send_json(self, msg: dict) -> None:
                self.sent.append(msg)

            async def close(self, **_kw) -> None:
                pass

        async def scenario() -> None:
            hub = WsHub()
            await hub.register_agent(AgentConn(
                ws=FakeWs(), agent_id="a1", user_id="u1", token_id="t1"))
            ui = FakeWs()
            await hub.register_ui(ui, "u1")  # type: ignore[arg-type]

            submit = asyncio.create_task(
                hub.submit_task("u1", "gate_summary", {}, timeout=5.0))
            await asyncio.sleep(0)
            rid = hub.agents["a1"].ws.sent[0]["request_id"]
            await hub.handle_result(rid, {"ok": True, "data": {"summary": {}}})
            result = await submit
            assert result["ok"]

            # 广播给 UI 的 task_done 必须含 op（读任务也带 —— 字段统一存在，
            # 过滤是前端职责）
            dones = [m for m in ui.sent if m.get("type") == "task_done"]
            assert dones, "UI 应收到 task_done 广播"
            assert dones[0]["op"] == "gate_summary"

        asyncio.run(scenario())

    def test_ui_register_gets_status_snapshot(self):
        """FR-2.1.5 回归：daemon 先连、UI 后连，注册即收到 daemon_status 快照。

        时序 bug：daemon_status 只在 agent 注册/断连瞬间广播；UI 晚于 daemon
        连接时若无快照，徽标永远停在初始 offline（配对成功但页面显示离线）。
        同时验证快照按 user 维度：u1 的 daemon 不能点亮 u2 的徽标。
        """
        import asyncio

        from skillos.api.ws_hub import AgentConn, WsHub

        class FakeWs:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            async def send_json(self, msg: dict) -> None:
                self.sent.append(msg)

            async def close(self, **_kw) -> None:
                pass

        async def scenario() -> None:
            hub = WsHub()
            # ① daemon(u1) 先连 —— 此时没有任何 UI 连接，广播落空
            agent_ws = FakeWs()
            await hub.register_agent(AgentConn(
                ws=agent_ws, agent_id="a1", user_id="u1", token_id="t1"))

            # ② UI(u1) 后连 —— 必须立即拿到 connected=true 快照
            ui1 = FakeWs()
            await hub.register_ui(ui1, "u1")  # type: ignore[arg-type]
            assert ui1.sent[0]["type"] == "daemon_status"
            assert ui1.sent[0]["connected"] is True

            # ③ UI(u2) 连 —— u1 的 daemon 不影响 u2（按 user 快照）
            ui2 = FakeWs()
            await hub.register_ui(ui2, "u2")  # type: ignore[arg-type]
            assert ui2.sent[0]["connected"] is False

            # ④ daemon 断连：只有 u1 的 UI 收到下线广播，u2 不受扰
            await hub.unregister_agent(hub.agents["a1"])
            types1 = [m["type"] for m in ui1.sent]
            assert "daemon_status" in types1
            assert all(m.get("type") != "daemon_status" for m in ui2.sent[1:])

        asyncio.run(scenario())

    def test_drift_event_upserts_and_aggregates(self, client, engine_home):
        """daemon drift_changed → 云端 upsert → GET /sync/status 按 Skill 聚合。"""
        engine, _ = engine_home
        token = _token(client)
        _pair(client, token)
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({"type": "hello", "agent_id": "test-agent",
                          "token": _agent_token(client, token)})
            ws.receive_json()
            from skillos.api.security import verify_jwt

            uid = verify_jwt(token, "test-secret")
            matrix = [
                {"slug": "demo", "runtime_key": "claude", "state": "SYNCED",
                 "name": "demo"},
                {"slug": "demo", "runtime_key": "agents", "state": "OUTDATED",
                 "name": "demo"},
                {"slug": "other", "runtime_key": "claude", "state": "UNMANAGED",
                 "name": "other"},
            ]
            ws.send_json({"type": "event", "event": "drift_changed",
                          "user_id": uid, "payload": {"matrix": matrix}})
            ws.send_json({"type": "ping"})
            # 重连自愈（FR-1.0.34）：register 后云端可能插入
            # runtime_sync_custom task —— 回 result 后继续等 pong
            # （真实 daemon 同样处理 task 再回 pong）
            for _ in range(3):
                msg = ws.receive_json()
                if msg.get("type") == "pong":
                    break
                if msg.get("type") == "task":
                    ws.send_json({
                        "type": "result",
                        "request_id": msg["request_id"],
                        "payload": {"ok": True, "data": {"synced": 0, "removed": 0}},
                    })
            else:
                raise AssertionError("3 条消息内未收到 pong")

        r = client.get("/api/sync/status", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        data = r.json()["data"]
        # WS 已断开 → live 降级为云端缓存（stale=true，FR-2.4.4 读保留）
        assert data["stale"] is True and data["daemon_online"] is False
        states = {row["slug"]: row["state"] for row in data["rows"]}
        assert states["demo"] == "OUTDATED"   # 组内取最严重
        assert states["other"] == "UNMANAGED"
        # 双读者统计（FR-2.1.1）：覆盖度 / 僵尸占比
        assert data["manager"]["total_skills"] == 2
        assert data["manager"]["zombie_pct"] == 50.0  # other=UNMANAGED
        assert data["manager"]["coverage_pct"] == 33.3  # 3 格中 1 格 synced

    def test_push_offline_503(self, client):
        token = _token(client)
        r = client.post("/api/sync/push",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"skill_ids": ["demo"]})
        assert r.status_code == 503
        assert "DAEMON_OFFLINE" in str(r.json())


def _agent_token(client: TestClient, jwt: str) -> str:
    """配对后从设备表拿不到明文 token（只存哈希）—— 测试走独立签发路径。"""
    # 直接用 CloudRepo 签发一枚测试 token（避开 WS 配对的明文一次性）
    from skillos.api.security import new_agent_token
    from skillos.store.cloud_repo import CloudRepo

    repo = CloudRepo(client.app.state.session_factory())
    user = repo.get_user_by_email("u@example.com")
    tid, plain, thash = new_agent_token()
    repo.issue_agent_token(user.id, "test-agent", tid, thash, platform="windows")
    return plain


class TestDaemonRetryClassification:
    """断线重连的异常分类（FR-1.2.x）。

    2026-10-25 实测：云端重启窗口里 `websockets.connect` 抛 `InvalidMessage`
    （did not receive a valid HTTP response），旧 except 列表只兜 ConnectionClosed /
    OSError / TimeoutError ⇒ daemon 进程直接退出，日志停在 ws_reconnect 后再无下文。
    把分类固定下来，别在改 except 列表时又漏掉握手异常。
    """

    def test_handshake_errors_are_retryable(self) -> None:
        from websockets.exceptions import InvalidMessage, InvalidStatus

        from skillos.daemon.main import _RETRY_ERRORS

        for exc_type in (InvalidMessage, InvalidStatus, ConnectionRefusedError,
                         TimeoutError, OSError):
            assert issubclass(exc_type, _RETRY_ERRORS), exc_type

    def test_bad_url_is_not_retried(self) -> None:
        """URL 拼错是配置错误：该当场失败，不该靠无限重试掩盖。"""
        from websockets.exceptions import InvalidURI

        from skillos.daemon.main import _RETRY_ERRORS

        assert not issubclass(InvalidURI, _RETRY_ERRORS)
