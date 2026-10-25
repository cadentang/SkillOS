"""M4-01 CR 状态机（E-05.1~3 / 5~6）。

两类断言分开：

- **纯函数**：迁移表、终态无出边、角色门槛、拒绝理由、门禁/STALE 判定 ——
  不碰网络，能一条一条列清楚"哪些边存在"；
- **HTTP 全链**：用 daemon 替身（`_FakeHub`）跑 `approve → merge` 与各失败分支，
  验的是"门禁失败真的退回 open 并写系统评论"、"canonical 前进真的 409"这类
  只在编排层才暴露的语义。

daemon 替身只回放 check / version_log 两个 op，不连 WS、不碰磁盘。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillos.api.main import create_app
from skillos.services.cr_service import (
    CR_TRANSITIONS,
    TERMINAL_STATUSES,
    CrError,
    MergeFacts,
    allowed_events,
    assert_role,
    next_status,
    transition,
)
from skillos.services.engine import LocalEngine, default_env

_SKILL_MD = (
    b"---\nname: demo\nversion: 1.0.0\n"
    b"description: a demo skill doing useful everyday work\n---\nbody\n"
)


# ---------------------------------------------------------------- 纯函数


class TestTransitionTable:
    def test_edges_are_explicit_and_terminal_have_none(self):
        """迁移表就是状态机的全部事实：终态必须一个出边都没有。"""
        assert CR_TRANSITIONS == {
            ("open", "approve"): "approved",
            ("open", "reject"): "rejected",
            ("open", "close"): "closed",
            ("approved", "merge"): "merged",
            ("approved", "close"): "closed",
        }
        for terminal in TERMINAL_STATUSES:
            assert allowed_events(terminal) == ()
            with pytest.raises(CrError) as ei:
                next_status(terminal, "approve")
            assert ei.value.code == "CR_TERMINAL"

    def test_open_cannot_merge_directly(self):
        with pytest.raises(CrError) as ei:
            next_status("open", "merge")
        assert ei.value.code == "INVALID_TRANSITION"
        assert ei.value.detail["allowed_events"] == ["approve", "reject", "close"]

    def test_reject_requires_reason(self):
        with pytest.raises(CrError) as ei:
            transition(
                status="open", event="reject", actor_id="u1", actor_role="member",
                author_id="u2", reason="   ",
            )
        assert ei.value.code == "REASON_REQUIRED"
        assert ei.value.status == 422

    def test_viewer_cannot_review(self):
        with pytest.raises(CrError) as ei:
            assert_role("approve", actor_id="u1", actor_role="viewer", author_id="u2")
        assert ei.value.status == 403

    def test_merge_and_close_need_author_or_owner(self):
        """处置类事件：同团队的其他 member 也不行（E-05.1）。"""
        with pytest.raises(CrError) as ei:
            assert_role("merge", actor_id="u9", actor_role="member", author_id="u2")
        assert ei.value.code == "FORBIDDEN"
        # owner 不受限；作者本人也行
        assert_role("merge", actor_id="u9", actor_role="owner", author_id="u2")
        assert_role("close", actor_id="u2", actor_role="member", author_id="u2")

    def test_gate_failure_blocks_merge(self):
        facts = MergeFacts(
            gate_has_error=True,
            gate_findings=({"rule": "fm.description.required", "level": "error",
                            "message": "缺 description"},),
            canonical_head="abc",
        )
        with pytest.raises(CrError) as ei:
            transition(
                status="approved", event="merge", actor_id="u1", actor_role="owner",
                author_id="u1", target_commit="abc", facts_provider=lambda: facts,
            )
        assert ei.value.code == "GATE_FAILED"
        assert ei.value.detail["revert_to"] == "open"
        assert ei.value.detail["findings"][0]["rule"] == "fm.description.required"

    def test_stale_when_canonical_advanced(self):
        facts = MergeFacts(False, (), canonical_head="new_head")
        with pytest.raises(CrError) as ei:
            transition(
                status="approved", event="merge", actor_id="u1", actor_role="owner",
                author_id="u1", target_commit="old_head", facts_provider=lambda: facts,
            )
        assert ei.value.code == "STALE"
        assert ei.value.detail["canonical_head"] == "new_head"

    def test_unreadable_head_never_passes(self):
        """读不到 HEAD 就是读不到：不许把"无法比对"当成"没变化"放行。"""
        facts = MergeFacts(False, (), canonical_head=None)
        with pytest.raises(CrError) as ei:
            transition(
                status="approved", event="merge", actor_id="u1", actor_role="owner",
                author_id="u1", target_commit="abc", facts_provider=lambda: facts,
            )
        assert ei.value.code == "CANNOT_VERIFY"

    def test_missing_target_commit_rejected(self):
        facts = MergeFacts(False, (), canonical_head="abc")
        with pytest.raises(CrError) as ei:
            transition(
                status="approved", event="merge", actor_id="u1", actor_role="owner",
                author_id="u1", target_commit=None, facts_provider=lambda: facts,
            )
        assert ei.value.code == "MISSING_TARGET_COMMIT"


# ---------------------------------------------------------------- HTTP 全链


class _FakeHub:
    """daemon 替身：只回放 check / version_log，并记录 UI 广播。"""

    def __init__(self, *, has_error: bool = False,
                 findings: tuple[dict, ...] = (), head: str | None = "head1",
                 offline: bool = False, gate_ok: bool = True) -> None:
        self.has_error = has_error
        self.findings = findings
        self.head = head
        self.offline = offline
        self.gate_ok = gate_ok
        self.calls: list[tuple[str, dict]] = []
        self.ui_events: list[dict] = []

    async def submit_task(self, user_id: str, op: str, params: dict) -> dict:
        if self.offline:
            raise ConnectionError("DAEMON_OFFLINE")
        self.calls.append((op, params))
        if op == "check":
            if not self.gate_ok:
                return {"ok": False, "error": {"code": "INVALID_INPUT", "message": "门禁炸了"}}
            return {"ok": True, "data": {
                "has_error": self.has_error, "findings": list(self.findings),
                "ruleset_hash": "h", "invalid_rules": [], "ignored": [], "decay": {},
            }}
        if op == "version_log":
            items = [{"sha": self.head}] if self.head else []
            return {"ok": True, "data": {"items": items}}
        raise AssertionError(f"未预期的 op: {op}")

    async def broadcast_ui(self, msg: dict, user_id: str | None = None) -> None:
        self.ui_events.append({**msg, "_user": user_id})


@pytest.fixture()
def app(tmp_path: Path):
    a = create_app(
        db_url=f"sqlite:///{tmp_path / 'cloud.db'}", jwt_secret="test-secret",
    )
    with TestClient(a) as c:
        yield c


def _reg(client: TestClient, email: str) -> str:
    client.post("/api/auth/register", json={"email": email, "password": "password123"})
    return client.post("/api/auth/login", json={
        "email": email, "password": "password123",
    }).json()["data"]["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _bootstrap_skill(client: TestClient, owner_email: str, slug: str = "demo") -> None:
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    home = tmp / "home"
    d = home / ".claude" / "skills" / slug
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(_SKILL_MD)
    engine = LocalEngine(default_env(home), home=home)
    engine.init_env()
    engine.import_skills()
    matrix = [{
        "slug": slug, "runtime_key": "claude", "state": "synced",
        "name": "demo", "description": "demo", "installed_version": "1.0.0",
    }]
    with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
        from skillos.store.cloud_repo import CloudRepo

        user = repo_user(s, owner_email)
        assert user is not None
        CloudRepo(s).upsert_matrix(user.id, matrix)


def repo_user(session, email: str):
    from skillos.store.cloud_repo import CloudRepo

    return CloudRepo(session).get_user_by_email(email)


def _setup(client: TestClient, *, target_commit: str | None = "head1",
           hub: _FakeHub | None = None) -> tuple[str, str, dict]:
    """团队 + skill + 一个 approved 的 CR，返回 (team_id, owner_token, cr)。"""
    owner = _reg(client, "o@x.com")
    client.app.state.hub = hub or _FakeHub()  # type: ignore[attr-defined]
    team_id = client.post("/api/teams", headers=_auth(owner),
                          json={"name": "CRT"}).json()["data"]["id"]
    _bootstrap_skill(client, "o@x.com")
    client.post(f"/api/teams/{team_id}/skills", headers=_auth(owner),
                json={"skill_slug": "demo"})
    cr = client.post(
        f"/api/teams/{team_id}/change-requests", headers=_auth(owner),
        json={"skill_slug": "demo", "title": "impl", "target_commit": target_commit},
    ).json()["data"]
    r = client.post(f"/api/teams/{team_id}/change-requests/{cr['id']}/review",
                    headers=_auth(owner), json={"action": "approve"})
    assert r.json()["data"]["status"] == "approved"
    return team_id, owner, cr


class TestMergeFlow:
    def test_merge_success_writes_audit_and_broadcast(self, app: TestClient):
        hub = _FakeHub(head="head1")
        team_id, owner, cr = _setup(app, target_commit="head1", hub=hub)

        r = app.post(f"/api/teams/{team_id}/change-requests/{cr['id']}/merge",
                     headers=_auth(owner), json={})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["status"] == "merged"
        assert r.json()["data"]["next_events"] == []

        # 门禁与 HEAD 都取过；门禁按 push 前置口径（不扫未纳管大文件）
        ops = [op for op, _ in hub.calls]
        assert ops == ["check", "version_log"]
        assert hub.calls[0][1]["include_untracked"] is False

        # 审计落 sync_records（合并 = 写操作）
        from skillos.store import models

        with app.app.state.session_factory() as s:  # type: ignore[attr-defined]
            recs = list(s.query(models.SyncRecord).all())
        assert any(r_.action == "push" and (r_.detail or {}).get("cr_id") == cr["id"]
                   for r_ in recs)

        # cr.updated 广播给团队成员（E-05.4）
        assert any(e["type"] == "cr.updated" and e["status"] == "merged"
                   and e["from"] == "approved" for e in hub.ui_events)

    def test_gate_failure_reverts_to_open_with_comment(self, app: TestClient):
        hub = _FakeHub(has_error=True, findings=(
            {"rule": "fm.description.required", "level": "error", "message": "缺描述"},
        ))
        team_id, owner, cr = _setup(app, target_commit="head1", hub=hub)

        r = app.post(f"/api/teams/{team_id}/change-requests/{cr['id']}/merge",
                     headers=_auth(owner), json={})
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == "GATE_FAILED" and detail["revert_to"] == "open"

        got = app.get(f"/api/teams/{team_id}/change-requests/{cr['id']}",
                      headers=_auth(owner)).json()["data"]
        assert got["status"] == "open"                     # E-05.2 退回 open
        assert got["target_commit"] == "head1"             # 提交事实不被抹掉
        sys_comments = [c for c in got["comments"] if c["author_id"] is None]
        assert sys_comments and "fm.description.required" in sys_comments[0]["body"]
        assert any(e["type"] == "cr.updated" and e["status"] == "open"
                   for e in hub.ui_events)

    def test_stale_when_canonical_advanced(self, app: TestClient):
        hub = _FakeHub(head="new_head")
        team_id, owner, cr = _setup(app, target_commit="old_head", hub=hub)

        r = app.post(f"/api/teams/{team_id}/change-requests/{cr['id']}/merge",
                     headers=_auth(owner), json={})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "STALE"

        got = app.get(f"/api/teams/{team_id}/change-requests/{cr['id']}",
                      headers=_auth(owner)).json()["data"]
        assert got["status"] == "approved"       # 状态不动，等用户刷新重来
        assert any("STALE" in c["body"] for c in got["comments"])

    def test_merge_daemon_offline(self, app: TestClient):
        hub = _FakeHub(offline=True)
        team_id, owner, cr = _setup(app, target_commit="head1", hub=hub)
        r = app.post(f"/api/teams/{team_id}/change-requests/{cr['id']}/merge",
                     headers=_auth(owner), json={})
        assert r.status_code == 503 and r.json()["detail"] == "DAEMON_OFFLINE"

    def test_gate_unavailable_blocks_merge(self, app: TestClient):
        hub = _FakeHub(gate_ok=False)
        team_id, owner, cr = _setup(app, target_commit="head1", hub=hub)
        r = app.post(f"/api/teams/{team_id}/change-requests/{cr['id']}/merge",
                     headers=_auth(owner), json={})
        assert r.status_code == 502
        assert r.json()["detail"]["code"] == "GATE_UNAVAILABLE"

    def test_cannot_verify_without_head(self, app: TestClient):
        hub = _FakeHub(head=None)
        team_id, owner, cr = _setup(app, target_commit="head1", hub=hub)
        r = app.post(f"/api/teams/{team_id}/change-requests/{cr['id']}/merge",
                     headers=_auth(owner), json={})
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "CANNOT_VERIFY"


class TestRejectAndAutoClose:
    def test_rejected_cr_still_accepts_comments(self, app: TestClient):
        owner = _reg(app, "o@x.com")
        app.app.state.hub = _FakeHub()  # type: ignore[attr-defined]
        team_id = app.post("/api/teams", headers=_auth(owner),
                           json={"name": "T"}).json()["data"]["id"]
        _bootstrap_skill(app, "o@x.com")
        app.post(f"/api/teams/{team_id}/skills", headers=_auth(owner),
                 json={"skill_slug": "demo"})
        cr = app.post(f"/api/teams/{team_id}/change-requests", headers=_auth(owner),
                      json={"skill_slug": "demo", "title": "x"}).json()["data"]

        r = app.post(f"/api/teams/{team_id}/change-requests/{cr['id']}/review",
                     headers=_auth(owner), json={"action": "reject"})
        assert r.status_code == 422                    # 驳回必须给理由

        r = app.post(f"/api/teams/{team_id}/change-requests/{cr['id']}/review",
                     headers=_auth(owner),
                     json={"action": "reject", "reason": "方向不对"})
        assert r.json()["data"]["status"] == "rejected"

        # E-05 验收：rejected 后仍可评论，状态不变
        r = app.post(f"/api/teams/{team_id}/change-requests/{cr['id']}/comments",
                     headers=_auth(owner), json={"body": "记一笔"})
        assert r.status_code == 200
        got = app.get(f"/api/teams/{team_id}/change-requests/{cr['id']}",
                      headers=_auth(owner)).json()["data"]
        assert got["status"] == "rejected"
        assert "方向不对" in [c["body"] for c in got["comments"]]

    def test_detach_skill_closes_open_crs(self, app: TestClient):
        """E-05.6：目标 Skill 离开团队 → 未终结 CR 自动关闭 + 系统评论。"""
        owner = _reg(app, "o@x.com")
        app.app.state.hub = _FakeHub()  # type: ignore[attr-defined]
        team_id = app.post("/api/teams", headers=_auth(owner),
                           json={"name": "T2"}).json()["data"]["id"]
        _bootstrap_skill(app, "o@x.com")
        app.post(f"/api/teams/{team_id}/skills", headers=_auth(owner),
                 json={"skill_slug": "demo"})
        cr = app.post(f"/api/teams/{team_id}/change-requests", headers=_auth(owner),
                      json={"skill_slug": "demo", "title": "y"}).json()["data"]

        r = app.delete(f"/api/teams/{team_id}/skills/demo", headers=_auth(owner))
        assert r.status_code == 200
        assert cr["id"] in r.json()["data"]["closed_crs"]

        got = app.get(f"/api/teams/{team_id}/change-requests/{cr['id']}",
                      headers=_auth(owner)).json()["data"]
        assert got["status"] == "closed"
        sys_comments = [c for c in got["comments"] if c["author_id"] is None]
        assert sys_comments and "自动关闭" in sys_comments[0]["body"]
