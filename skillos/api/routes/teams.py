"""P6 协作 API（FR-3.7 / FR-2.3.x）。

权限边界（FR-2.3.4，2026/10 P6 §3.2）：
- viewer：GET 类
- member：创建 CR / 评论
- owner：成员管理 / 审查 CR / 改名 / 合并
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from skillos.api.deps import get_current_user_id, get_repo
from skillos.api.services.teams import (
    CR_STATUSES,
    require_admin,
    require_viewer,
    require_writer,
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
    return {"ok": True, "data": {"detached": True}, "error": None}


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
    action: str  # "merged" | "closed"


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
def review_cr(
    team_id: str, cr_id: str, body: ReviewCrBody,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    """owner 审查：merged = 合入，closed = 驳回；仅 open 状态可审查（FR-3.16）。"""
    require_admin(repo, team_id, user_id)
    if body.action not in ("merged", "closed"):
        raise HTTPException(status_code=422, detail="INVALID_ACTION")
    try:
        cr = repo.set_cr_status(cr_id, body.action, user_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    if cr is None or cr.team_id != team_id:
        raise HTTPException(status_code=404, detail="CR_NOT_FOUND")
    # CR 状态变更落 sync_records（应用层审计：合并/关闭即写操作）
    repo.record_sync(
        action="push" if body.action == "merged" else "resolve",
        skill_id=cr.skill_id, runtime_id=None,
        detail={"cr_id": cr_id, "action": body.action,
                "target_commit": cr.target_commit},
        by=user_id,
    )
    return {"ok": True, "data": _cr_payload(
        cr, comment_count=len(repo.list_cr_comments(cr_id))), "error": None}


# ============================================================ 管理者视图（FR-3.17）


@router.get("/{team_id}/dashboard")
def team_dashboard(
    team_id: str, user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    require_viewer(repo, team_id, user_id)
    return {"ok": True, "data": repo.team_dashboard(team_id), "error": None}


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