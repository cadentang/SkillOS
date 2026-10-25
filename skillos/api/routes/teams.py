"""P6 协作 API（FR-3.7 / FR-2.3.x）。

权限边界（FR-2.3.4，2026/10 P6 §3.2）：
- viewer：GET 类
- member：创建 CR / 评论
- owner：成员管理 / 审查 CR / 改名 / 合并
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from skillos.api.deps import get_current_user_id, get_repo
from skillos.api.services.teams import (
    CR_STATUSES,
    require_admin,
    require_viewer,
    require_writer,
)
from skillos.services.cr_service import (
    CrError,
    CrService,
    MergeFacts,
    allowed_events,
    assert_role,
    failure_comment,
    next_status,
    transition,
)
from skillos.store import models
from skillos.store.cloud_repo import CloudRepo

router = APIRouter(prefix="/teams", tags=["teams"])


# ============================================================ 团队 CRUD


class CreateTeamBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class RenameTeamBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.post("")
def create_team(
    body: CreateTeamBody, user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    t = repo.create_team(body.name.strip(), user_id)
    owner_member = models.TeamMember(team_id=t.id, user_id=user_id, role="owner")
    return {"ok": True, "data": _team_payload(
        t, role="owner", members=[owner_member], crs=[],
    ), "error": None}


@router.get("")
def list_my_teams(
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    rows = repo.list_user_teams(user_id)
    out = []
    for t in rows:
        m = repo.get_membership(t.id, user_id)
        out.append(_team_payload(t, role=m.role if m else "viewer"))
    return {"ok": True, "data": {"items": out}, "error": None}


@router.get("/{team_id}")
def get_team(
    team_id: str, user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    role_info = require_viewer(repo, team_id, user_id)
    t = repo.get_team(team_id)
    if t is None:
        raise HTTPException(status_code=404, detail="TEAM_NOT_FOUND")
    return {"ok": True,
            "data": _team_payload(t, role=role_info[1], members=repo.list_members(team_id),
                                  crs=repo.list_team_crs(team_id)), "error": None}


@router.patch("/{team_id}")
def rename_team(
    team_id: str, body: RenameTeamBody,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    require_admin(repo, team_id, user_id)
    t = repo.rename_team(team_id, body.name.strip())
    if t is None:
        raise HTTPException(status_code=404, detail="TEAM_NOT_FOUND")
    return {"ok": True, "data": _team_payload(t, role="owner"), "error": None}


# ============================================================ 成员管理


class AddMemberBody(BaseModel):
    email: str  # 通过 email 邀请
    role: str = "member"


@router.post("/{team_id}/members")
def add_member(
    team_id: str, body: AddMemberBody,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    require_admin(repo, team_id, user_id)
    if body.role not in ("owner", "member", "viewer"):
        raise HTTPException(status_code=422, detail="INVALID_ROLE")
    target = repo.get_user_by_email(body.email.strip())
    if target is None:
        raise HTTPException(status_code=404, detail="USER_NOT_FOUND")
    m = repo.add_member(team_id, target.id, body.role)
    return {"ok": True, "data": _member_payload(m, target), "error": None}


@router.patch("/{team_id}/members/{member_user_id}")
def change_role(
    team_id: str, member_user_id: str, body: AddMemberBody,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    require_admin(repo, team_id, user_id)
    if body.role not in ("owner", "member", "viewer"):
        raise HTTPException(status_code=422, detail="INVALID_ROLE")
    m = repo.add_member(team_id, member_user_id, body.role)
    return {"ok": True, "data": _member_payload(m), "error": None}


@router.delete("/{team_id}/members/{member_user_id}")
def remove_member(
    team_id: str, member_user_id: str,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    require_admin(repo, team_id, user_id)
    if member_user_id == user_id:
        raise HTTPException(status_code=422, detail="CANNOT_REMOVE_SELF")
    ok_removed = repo.remove_member(team_id, member_user_id)
    if not ok_removed:
        raise HTTPException(status_code=422, detail="CANNOT_REMOVE_OWNER")
    return {"ok": True, "data": {"removed": True}, "error": None}


@router.get("/{team_id}/members")
def list_members(
    team_id: str, user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    require_viewer(repo, team_id, user_id)
    out = []
    for m in repo.list_members(team_id):
        u = repo.get_user(m.user_id)
        out.append(_member_payload(m, u))
    return {"ok": True, "data": {"items": out}, "error": None}


# ============================================================ Skill 归属团队（FR-2.3.2）


class AttachSkillBody(BaseModel):
    skill_slug: str


@router.post("/{team_id}/skills")
def attach_skill(
    team_id: str, body: AttachSkillBody,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    require_admin(repo, team_id, user_id)
    skill = repo.get_skill_by_slug(body.skill_slug)
    if skill is None:
        raise HTTPException(status_code=404, detail="SKILL_NOT_FOUND")
    if skill.owner_id and skill.owner_id != user_id:
        raise HTTPException(status_code=403, detail="NOT_OWNER")
    try:
        skill = repo.attach_skill_to_team(skill.id, team_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="SKILL_NOT_FOUND") from None
    return {"ok": True, "data": {"skill_id": skill.id, "team_id": team_id,
                                  "visibility": skill.visibility}, "error": None}


@router.delete("/{team_id}/skills/{skill_slug}")
def detach_skill(
    team_id: str, skill_slug: str,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    require_admin(repo, team_id, user_id)
    skill = repo.get_skill_by_slug(skill_slug)
    if skill is None or skill.team_id != team_id:
        raise HTTPException(status_code=404, detail="SKILL_NOT_IN_TEAM")
    try:
        repo.detach_skill_from_team(skill.id)
    except KeyError:
        raise HTTPException(status_code=404, detail="SKILL_NOT_FOUND") from None
    # E-05.6：Skill 离开团队后，指向它的未终结 CR 永远合不上 —— 自动关闭 + 系统评论。
    # 云端的"删除"就是 detach（云端不碰磁盘，本地删除由 daemon 处理，这里感知不到）。
    closed = CrService(repo).close_for_deleted_skill(
        skill.id, by=user_id, reason="目标 Skill 已移出团队",
    )
    if closed:
        repo.record_sync(
            action="resolve", skill_id=skill.id, runtime_id=None,
            detail={"event": "cr_auto_closed", "cr_ids": closed}, by=user_id,
        )
    return {"ok": True, "data": {"detached": True, "closed_crs": closed}, "error": None}


@router.get("/{team_id}/skills")
def list_team_skills(
    team_id: str, user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    require_viewer(repo, team_id, user_id)
    items = repo.list_team_skills(team_id)
    return {"ok": True, "data": {"items": [
        {"id": s.id, "slug": s.slug, "name": s.name, "description": s.description,
         "visibility": s.visibility} for s in items
    ]}, "error": None}


# ============================================================ Change Request（FR-2.3.3）


class CreateCrBody(BaseModel):
    skill_slug: str
    title: str = Field(min_length=1, max_length=200)
    body: str | None = None
    base_version: str | None = None
    target_commit: str | None = None


class ReviewCrBody(BaseModel):
    """审查事件（E-05.1）：approve / reject / close。

    reject 必须带 `reason`（落 cr_comments）；旧版 `merged|closed` 取值已取消 ——
    合并是独立端点（要走门禁与 STALE 比对），不再混进审查动作里。
    """

    action: str  # approve | reject | close
    reason: str | None = Field(default=None, max_length=8000)


class MergeCrBody(BaseModel):
    """合并请求（E-05.3）：`base_hash` = 客户端提交 CR 时看到的 canonical commit。"""

    base_hash: str | None = None


class CommentBody(BaseModel):
    body: str = Field(min_length=1, max_length=8000)


@router.post("/{team_id}/change-requests")
def create_cr(
    team_id: str, body: CreateCrBody,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    require_writer(repo, team_id, user_id)
    skill = repo.get_skill_by_slug(body.skill_slug)
    if skill is None or skill.team_id != team_id:
        raise HTTPException(status_code=404, detail="SKILL_NOT_IN_TEAM")
    cr = repo.create_change_request(
        team_id=team_id, skill_id=skill.id, title=body.title.strip(),
        body=body.body, author_id=user_id,
        base_version=body.base_version, target_commit=body.target_commit,
    )
    return {"ok": True, "data": _cr_payload(cr, comment_count=0), "error": None}


@router.get("/{team_id}/change-requests")
def list_crs(
    team_id: str, status: str | None = None,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    require_viewer(repo, team_id, user_id)
    if status and status not in CR_STATUSES:
        raise HTTPException(status_code=422, detail="INVALID_STATUS")
    crs = repo.list_team_crs(team_id, status=status)
    items = []
    for cr in crs:
        items.append(_cr_payload(cr, comment_count=len(repo.list_cr_comments(cr.id))))
    return {"ok": True, "data": {"items": items}, "error": None}


@router.get("/{team_id}/change-requests/{cr_id}")
def get_cr(
    team_id: str, cr_id: str,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    require_viewer(repo, team_id, user_id)
    cr = repo.get_change_request(cr_id)
    if cr is None or cr.team_id != team_id:
        raise HTTPException(status_code=404, detail="CR_NOT_FOUND")
    return {"ok": True, "data": _cr_payload(
        cr, comment_count=len(repo.list_cr_comments(cr_id)),
        comments=[_comment_payload(c) for c in repo.list_cr_comments(cr_id)],
    ), "error": None}


@router.post("/{team_id}/change-requests/{cr_id}/comments")
def add_comment(
    team_id: str, cr_id: str, body: CommentBody,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    require_writer(repo, team_id, user_id)
    cr = repo.get_change_request(cr_id)
    if cr is None or cr.team_id != team_id:
        raise HTTPException(status_code=404, detail="CR_NOT_FOUND")
    try:
        c = repo.add_cr_comment(cr_id, user_id, body.body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return {"ok": True, "data": _comment_payload(c), "error": None}


@router.post("/{team_id}/change-requests/{cr_id}/review")
async def review_cr(
    team_id: str, cr_id: str, body: ReviewCrBody, request: Request,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    """审查（E-05.1）：approve → approved / reject → rejected（必须理由）/ close → closed。

    审查权 = owner 或 member（viewer → 403）；判定全在 `cr_service.transition`，
    这里只做取数、落库与广播。
    """
    _, role = require_writer(repo, team_id, user_id)
    cr = _get_team_cr(repo, team_id, cr_id)
    if body.action not in ("approve", "reject", "close"):
        raise HTTPException(status_code=422, detail={
            "code": "INVALID_ACTION",
            "allowed_events": list(allowed_events(cr.status)),
        })

    try:
        plan = transition(
            status=cr.status, event=body.action, actor_id=user_id,
            actor_role=role, author_id=cr.author_id, reason=body.reason,
        )
    except CrError as e:
        raise _http(e) from None

    updated = CrService(repo).apply(cr, plan, by=user_id)
    await _broadcast_cr(request, repo, team_id, updated, event=plan.event,
                        from_status=plan.from_status)
    return {"ok": True, "data": _cr_payload(
        updated, comment_count=len(repo.list_cr_comments(cr_id))), "error": None}


@router.post("/{team_id}/change-requests/{cr_id}/merge")
async def merge_cr(
    team_id: str, cr_id: str, body: MergeCrBody, request: Request,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    """合并（E-05.2/3/5）：门禁前置 + STALE 比对，通过后才落 `merged`。

    合并不是"点一下状态变一下"：云端先把事实取回来（daemon 的 gate + canonical
    HEAD），再由 `cr_service.transition` 判定。门禁失败 → CR 退回 `open` 并写
    GATE_FAILED 明细；canonical 已前进 → 409 STALE，让用户刷新重提交。
    """
    _, role = require_writer(repo, team_id, user_id)
    cr = _get_team_cr(repo, team_id, cr_id)

    # 先做无 I/O 的边/角色判定：注定被拒的请求不该白跑一次全库门禁（贵且有落库副作用）
    try:
        next_status(cr.status, "merge")
        assert_role("merge", actor_id=user_id, actor_role=role, author_id=cr.author_id)
    except CrError as e:
        raise _http(e) from None

    skill = repo.get_skill(cr.skill_id)
    if skill is None:
        # E-05.6：目标 Skill 不在了，这个 CR 永远合不上 —— 自动关闭并说明原因
        CrService(repo).close_for_deleted_skill(
            cr.skill_id, by=user_id, reason="合并时发现目标 Skill 已不存在",
        )
        raise HTTPException(status_code=409, detail={
            "code": "CR_SKILL_GONE", "message": "目标 Skill 已不存在，CR 已自动关闭",
        })

    facts = await _merge_facts(request, user_id, skill.slug)
    target = (body.base_hash or cr.target_commit or "").strip()
    try:
        plan = transition(
            status=cr.status, event="merge", actor_id=user_id, actor_role=role,
            author_id=cr.author_id, target_commit=target or None,
            facts_provider=lambda: facts,
        )
    except CrError as e:
        if e.code == "GATE_FAILED":
            # E-05.2：拒绝合并 + 退回 open + GATE_FAILED 明细进评论区
            CrService(repo).revert_after_gate_failure(
                cr, by=user_id, comment=failure_comment(e) or "系统：GATE_FAILED",
            )
            await _broadcast_cr(request, repo, team_id, cr, event="merge",
                                from_status="approved")
        elif e.code == "STALE":
            comment = failure_comment(e)
            if comment:
                repo.add_cr_comment(cr_id, None, comment)
        raise _http(e) from None

    updated = CrService(repo).apply(cr, plan, by=user_id)
    await _broadcast_cr(request, repo, team_id, updated, event=plan.event,
                        from_status=plan.from_status)
    return {"ok": True, "data": _cr_payload(
        updated, comment_count=len(repo.list_cr_comments(cr_id))), "error": None}


# ============================================================ 管理者视图（FR-3.17）


@router.get("/{team_id}/dashboard")
def team_dashboard(
    team_id: str, user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    require_viewer(repo, team_id, user_id)
    return {"ok": True, "data": repo.team_dashboard(team_id), "error": None}


# ============================================================ CR 迁移辅助


def _get_team_cr(repo: CloudRepo, team_id: str, cr_id: str) -> models.ChangeRequest:
    cr = repo.get_change_request(cr_id)
    if cr is None or cr.team_id != team_id:
        raise HTTPException(status_code=404, detail="CR_NOT_FOUND")
    return cr


def _http(err: CrError) -> HTTPException:
    """CrError → HTTPException：code/message/detail 一起透出，前端不用猜。"""
    detail: dict = {"code": err.code, "message": err.message}
    detail.update(err.detail)
    return HTTPException(status_code=err.status, detail=detail)


async def _merge_facts(request: Request, user_id: str, slug: str) -> MergeFacts:
    """取合并所需的两项本地事实（E-05.2 / E-05.3）。

    - 门禁走 daemon `check` 且 `include_untracked=False` —— 与 push 前置同一口径
      （E-03.4 / NFR-10.5），不为了合并多读一次磁盘大文件；
    - canonical HEAD 取该 Skill 的提交序列首条。**读不到就是读不到**：返回
      `canonical_head=None`，由 `transition` 判 CANNOT_VERIFY，不假装通过。
    """
    hub = request.app.state.hub
    try:
        gate = await hub.submit_task(user_id, "check", {
            "slug": slug, "security_only": False, "include_untracked": False,
        })
        log = await hub.submit_task(user_id, "version_log", {"slug": slug, "limit": 1})
    except ConnectionError:
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE") from None

    if not gate.get("ok"):
        err = gate.get("error") or {}
        raise HTTPException(status_code=502, detail={
            "code": "GATE_UNAVAILABLE",
            "message": err.get("message") or "门禁读取失败，拒绝在无门禁结论时合并",
        })
    data = gate.get("data") or {}
    findings = tuple(data.get("findings") or ())

    head: str | None = None
    if log.get("ok"):
        items = (log.get("data") or {}).get("items") or []
        if items:
            head = items[0].get("sha") or None
    return MergeFacts(
        gate_has_error=bool(data.get("has_error")),
        gate_findings=findings,
        canonical_head=head,
    )


async def _broadcast_cr(request: Request, repo: CloudRepo, team_id: str,
                        cr: models.ChangeRequest, *, event: str,
                        from_status: str) -> None:
    """`cr.updated`（E-05.4）：只发给该团队成员，不做跨租户广播。

    推送是尽力而为 —— 丢了前端还有查询兜底；但**必须**带 from/to，否则列表页
    分不清"我刚点的"与"别人改的"。
    """
    hub = getattr(request.app.state, "hub", None)
    if hub is None:
        return
    payload = {
        "type": "cr.updated", "team_id": team_id, "cr_id": cr.id,
        "status": cr.status, "event": event, "from": from_status,
    }
    for m in repo.list_members(team_id):
        await hub.broadcast_ui(payload, user_id=m.user_id)


# ============================================================ 内部序列化


def _team_payload(t, *, role: str, members=None, crs=None) -> dict:
    return {
        "id": t.id, "name": t.name, "owner_id": t.owner_id,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "role": role,
        "members": [_member_payload(m) for m in (members or [])],
        "crs": [_cr_payload(c, 0) for c in (crs or [])],
    }


def _member_payload(m, u=None) -> dict:
    return {
        "user_id": m.user_id, "role": m.role,
        "email": u.email if u else None, "name": u.name if u else None,
    }


def _cr_payload(cr, *, comment_count: int, comments=None) -> dict:
    return {
        "id": cr.id, "team_id": cr.team_id, "skill_id": cr.skill_id,
        "title": cr.title, "body": cr.body, "status": cr.status,
        "author_id": cr.author_id, "reviewer_id": cr.reviewer_id,
        "base_version": cr.base_version, "target_commit": cr.target_commit,
        # 迁移表推导，前端按钮不再自己抄一份状态机（E-05.1）
        "next_events": list(allowed_events(cr.status)),
        "comment_count": comment_count,
        "comments": comments or [],
        "created_at": cr.created_at.isoformat() if cr.created_at else None,
        "updated_at": cr.updated_at.isoformat() if cr.updated_at else None,
    }


def _comment_payload(c) -> dict:
    return {
        "id": c.id, "cr_id": c.cr_id, "author_id": c.author_id,
        "body": c.body, "at": c.at.isoformat() if c.at else None,
    }


# 路由集中暴露 30+ 端点：pre-set import 兼容性
__all__ = ["router"]