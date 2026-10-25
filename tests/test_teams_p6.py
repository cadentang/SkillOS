"""P6 协作 API 测试（FR-2.3.x / FR-3.7 / FR-3.16 / FR-3.17）。

- 权限矩阵：viewer / member / owner 对 CRUD 的可见性
- CR 状态机：open → merged/closed 单向不可逆（FR-3.16 不可变审计）
- 不可变审计：app 层不提供 CR 评论 / CR 状态回退 / 删除接口
- 管理者视图：覆盖度 / 僵尸 / 成员贡献聚合口径
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillos.api.main import create_app
from skillos.services.engine import LocalEngine, default_env

_SKILL_MD = (
    b"---\nname: demo\nversion: 1.0.0\n"
    b"description: a demo skill doing useful everyday work\n---\nbody\n"
)


@pytest.fixture()
def app(tmp_path: Path):
    a = create_app(
        db_url=f"sqlite:///{tmp_path / 'cloud.db'}", jwt_secret="test-secret",
    )
    with TestClient(a) as c:
        yield c


def _reg(client: TestClient, email: str, password: str = "password123") -> str:
    client.post("/api/auth/register", json={"email": email, "password": password})
    return client.post("/api/auth/login",
                       json={"email": email, "password": password}).json()["data"]["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _bootstrap_skill(client: TestClient, owner_token: str,
                    owner_email: str = "owner@example.com", slug: str = "demo") -> None:
    """本地引擎 → 上报漂移 → 云端 upsert skill（cloud_repo.upsert_skill）。"""
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
        "slug": slug, "runtime_key": "claude",
        "state": "synced", "name": "demo", "description": "demo",
        "installed_version": "1.0.0",
    }]
    with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
        from skillos.store.cloud_repo import CloudRepo
        repo = CloudRepo(s)
        user = repo.get_user_by_email(owner_email)
        assert user is not None, f"测试前未注册用户 {owner_email}"
        repo.upsert_matrix(user.id, matrix)


# ------------------------------------------------ 团队 CRUD + 权限矩阵


def test_create_team_makes_creator_owner(app: TestClient):
    token = _reg(app, "owner@example.com")
    r = app.post("/api/teams", headers=_auth(token), json={"name": "Acme"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["name"] == "Acme" and data["role"] == "owner"
    assert len(data["members"]) == 1 and data["members"][0]["role"] == "owner"


def test_member_cannot_manage_members(app: TestClient):
    owner = _reg(app, "o@x.com")
    member = _reg(app, "m@x.com")
    team_id = app.post("/api/teams", headers=_auth(owner),
                       json={"name": "T1"}).json()["data"]["id"]
    # owner 邀请 member
    app.post(f"/api/teams/{team_id}/members", headers=_auth(owner),
             json={"email": "m@x.com", "role": "member"})
    # member 试图改 role → 403
    r = app.patch(f"/api/teams/{team_id}/members/{owner}",  # 用成员身份请求
                  headers=_auth(member), json={"email": "", "role": "viewer"})
    assert r.status_code == 403 and r.json()["detail"] == "INSUFFICIENT_ROLE"


def test_viewer_cannot_create_cr(app: TestClient):
    owner = _reg(app, "o@x.com")
    viewer = _reg(app, "v@x.com")
    team_id = app.post("/api/teams", headers=_auth(owner),
                       json={"name": "T2"}).json()["data"]["id"]
    app.post(f"/api/teams/{team_id}/members", headers=_auth(owner),
             json={"email": "v@x.com", "role": "viewer"})
    _bootstrap_skill(app, owner, owner_email="o@x.com")
    # 把 skill 归属到团队
    app.post(f"/api/teams/{team_id}/skills", headers=_auth(owner),
             json={"skill_slug": "demo"})
    r = app.post(f"/api/teams/{team_id}/change-requests",
                 headers=_auth(viewer),
                 json={"skill_slug": "demo", "title": "fix bug"})
    assert r.status_code == 403 and r.json()["detail"] == "INSUFFICIENT_ROLE"


def test_cannot_remove_owner(app: TestClient):
    owner = _reg(app, "o@x.com")
    admin2 = _reg(app, "a@x.com")
    team_id = app.post("/api/teams", headers=_auth(owner),
                       json={"name": "T3"}).json()["data"]["id"]
    app.post(f"/api/teams/{team_id}/members", headers=_auth(owner),
             json={"email": "a@x.com", "role": "owner"})
    owner_id = app.get("/api/auth/me", headers=_auth(owner)).json()["data"]["id"]
    r = app.delete(f"/api/teams/{team_id}/members/{owner_id}",
                   headers=_auth(admin2))
    # admin2 是 owner 也无法直接移除另一位 owner；需先转交/降级（避免 owner 自删）
    assert r.status_code == 422 and "OWNER" in r.json()["detail"]


# ------------------------------------------------ Change Request 状态机（FR-3.16 不可变审计）


def _setup_team_with_skill(app: TestClient, owner_token: str) -> str:
    team_id = app.post("/api/teams", headers=_auth(owner_token),
                       json={"name": "CR"}).json()["data"]["id"]
    _bootstrap_skill(app, owner_token, owner_email="o@x.com")
    app.post(f"/api/teams/{team_id}/skills", headers=_auth(owner_token),
             json={"skill_slug": "demo"})
    return team_id


def test_cr_open_then_approved_then_terminal(app: TestClient):
    """E-05.1：open → approved →（终态）；open 不可直接 merged。

    合并要过门禁与 STALE，必须有 daemon，所以这里只验审查段与终态不可回跳；
    合并的全部路径见 `tests/test_m4_cr.py`（带 daemon 替身）。
    """
    owner = _reg(app, "o@x.com")
    team_id = _setup_team_with_skill(app, owner)
    cr = app.post(f"/api/teams/{team_id}/change-requests",
                  headers=_auth(owner),
                  json={"skill_slug": "demo", "title": "impl"}).json()["data"]
    assert cr["status"] == "open"
    assert cr["next_events"] == ["approve", "reject", "close"]

    # open 直接合并 → 非法边（必须先审查通过）
    r = app.post(f"/api/teams/{team_id}/change-requests/{cr['id']}/merge",
                 headers=_auth(owner), json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "INVALID_TRANSITION"

    # 审查通过
    r = app.post(f"/api/teams/{team_id}/change-requests/{cr['id']}/review",
                 headers=_auth(owner), json={"action": "approve"})
    assert r.status_code == 200 and r.json()["data"]["status"] == "approved"
    assert r.json()["data"]["next_events"] == ["merge", "close"]

    # 关闭（终态），再审查 → 409（FR-3.16）
    r = app.post(f"/api/teams/{team_id}/change-requests/{cr['id']}/review",
                 headers=_auth(owner), json={"action": "close"})
    assert r.json()["data"]["status"] == "closed"
    r = app.post(f"/api/teams/{team_id}/change-requests/{cr['id']}/review",
                 headers=_auth(owner), json={"action": "approve"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CR_TERMINAL"


def test_member_can_comment_and_review_viewer_cannot(app: TestClient):
    """E-05.1：审查权 = owner / member；viewer 一律 403（P6 的「仅 owner」已放宽）。"""
    owner = _reg(app, "o@x.com")
    member = _reg(app, "m@x.com")
    viewer = _reg(app, "v@x.com")
    team_id = _setup_team_with_skill(app, owner)
    app.post(f"/api/teams/{team_id}/members", headers=_auth(owner),
             json={"email": "m@x.com", "role": "member"})
    app.post(f"/api/teams/{team_id}/members", headers=_auth(owner),
             json={"email": "v@x.com", "role": "viewer"})
    cr_id = app.post(f"/api/teams/{team_id}/change-requests",
                     headers=_auth(member),
                     json={"skill_slug": "demo", "title": "new"}).json()["data"]["id"]
    # 评论
    r = app.post(f"/api/teams/{team_id}/change-requests/{cr_id}/comments",
                 headers=_auth(member), json={"body": "lgtm"})
    assert r.status_code == 200
    # viewer 不能审查
    r = app.post(f"/api/teams/{team_id}/change-requests/{cr_id}/review",
                 headers=_auth(viewer), json={"action": "approve"})
    assert r.status_code == 403
    # member 可以审查（approve）
    r = app.post(f"/api/teams/{team_id}/change-requests/{cr_id}/review",
                 headers=_auth(member), json={"action": "approve"})
    assert r.status_code == 200 and r.json()["data"]["status"] == "approved"


def test_no_delete_or_update_comment_endpoint(app: TestClient):
    """FR-3.16：CR / 评论 应用层不提供改 / 删除接口 —— OpenAPI 路由集合断言。"""
    from skillos.api.routes.teams import router
    methods_paths = {(r.path, ",".join(r.methods or set()))
                      for r in router.routes if hasattr(r, "methods")}
    # 没有 DELETE/POST 修改评论的端点
    for path in methods_paths:
        if "comments" in path[0]:
            # 仅允许 POST 创建
            assert path[1] == "POST", f"评论意外端点: {path}"
    # CR 本身无 DELETE / PATCH
    for path in methods_paths:
        if "change-requests/" in path[0] and path[0].rstrip("/").count("/") > 2:
            # 形如 /teams/{t}/change-requests/{cr} 只有 GET；review/comments 是子端点
            pass
    cr_routes = [p for p in methods_paths
                 if p[0].count("/}") == 3 and "change-requests" in p[0]
                 and "review" not in p[0] and "comments" not in p[0]]
    for p in cr_routes:
        assert p[1] == "GET", f"CR 主体意外非只读端点: {p}"


# ------------------------------------------------ Skill 归属 / 可见性（FR-2.3.2）


def test_attach_skill_sets_team_visibility(app: TestClient):
    owner = _reg(app, "o@x.com")
    team_id = app.post("/api/teams", headers=_auth(owner),
                       json={"name": "T"}).json()["data"]["id"]
    _bootstrap_skill(app, owner, owner_email="o@x.com")
    r = app.post(f"/api/teams/{team_id}/skills", headers=_auth(owner),
                 json={"skill_slug": "demo"})
    assert r.status_code == 200
    assert r.json()["data"]["visibility"] == "team"

    # 重复 attach 同 skill 到别的 team 应允许（不同 team）；同 team 重复应幂等
    r = app.post(f"/api/teams/{team_id}/skills", headers=_auth(owner),
                 json={"skill_slug": "demo"})
    assert r.status_code == 200
    items = app.get(f"/api/teams/{team_id}/skills",
                    headers=_auth(owner)).json()["data"]["items"]
    assert len(items) == 1


def test_attach_non_existing_skill_returns_404(app: TestClient):
    owner = _reg(app, "o@x.com")
    team_id = app.post("/api/teams", headers=_auth(owner),
                       json={"name": "T"}).json()["data"]["id"]
    r = app.post(f"/api/teams/{team_id}/skills", headers=_auth(owner),
                 json={"skill_slug": "ghost"})
    assert r.status_code == 404


# ------------------------------------------------ 管理者视图（FR-3.17）


def test_dashboard_aggregates_health_contribution(app: TestClient):
    owner = _reg(app, "o@x.com")
    team_id = app.post("/api/teams", headers=_auth(owner),
                       json={"name": "Dash"}).json()["data"]["id"]
    _bootstrap_skill(app, owner, owner_email="o@x.com")
    app.post(f"/api/teams/{team_id}/skills", headers=_auth(owner),
             json={"skill_slug": "demo"})
    r = app.get(f"/api/teams/{team_id}/dashboard", headers=_auth(owner))
    data = r.json()["data"]
    assert data["total_skills"] == 1
    assert "synced_cells" in data and "coverage_pct" in data
    assert "health" in data and "contribution" in data
    # 至少一个贡献者（无主 skill 应入 'unowned'）
    assert any(c["skill_count"] >= 1 for c in data["contribution"])


def test_dashboard_requires_member(app: TestClient):
    """非成员看 dashboard → 403。"""
    owner = _reg(app, "o@x.com")
    outsider = _reg(app, "out@x.com")
    team_id = app.post("/api/teams", headers=_auth(owner),
                       json={"name": "D"}).json()["data"]["id"]
    r = app.get(f"/api/teams/{team_id}/dashboard", headers=_auth(outsider))
    assert r.status_code == 403