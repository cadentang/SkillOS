"""Skill CRUD（P3）：新建（FR-2.1.7）/ 删除（removals 守卫 FR-1.0.26 ②）。

全部经 daemon 任务下发（NFR-1：真值在本地 git + 磁盘，云端不碰磁盘）。
校验双保险：路由侧 kebab 快速失败（422），引擎侧仍全量校验（SLUG_EXISTS
含 path_key 归一化比对，路由侧不做重复实现以免两处判定漂移）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from skillos.api.deps import get_current_user_id
from skillos.core.algorithm.checks import is_kebab

router = APIRouter(prefix="/skills", tags=["skills"])


class SkillCreateRequest(BaseModel):
    slug: str
    name: str = ""
    description: str
    author: str = "web <web@skillos.local>"


class SkillDeleteRequest(BaseModel):
    dry_run: bool = True
    confirm_removals: list[str] = []
    author: str = "web <web@skillos.local>"


async def _submit(request: Request, user_id: str, op: str, params: dict) -> dict:
    """统一任务下发：离线 503，result 信封透传。"""
    try:
        result = await request.app.state.hub.submit_task(user_id, op, params, timeout=60.0)
    except ConnectionError:
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE") from None
    if not result.get("ok"):
        err = result.get("error") or {}
        return {
            "ok": False,
            "error": {"code": err.get("code", "INTERNAL"),
                      "message": err.get("message", "")},
            "removals": result.get("removals", []),
            "data": None,
        }
    return {"ok": True, "data": result.get("data"), "error": None}


@router.post("")
async def skill_create(body: SkillCreateRequest, request: Request,
                       user_id: str = Depends(get_current_user_id)) -> dict:
    """新建 Skill（FR-2.1.7）：canonical 模板 + commit + 索引，不自动 push。

    入口校验失败返回信封错误（与 daemon 透传的 SLUG_INVALID 同构），
    不抛 HTTPException——前端按 envelope.error.code 统一分支。
    """
    if not is_kebab(body.slug):
        return {"ok": False,
                "error": {"code": "SLUG_INVALID",
                          "message": "slug 必须是 kebab-case（小写字母数字 + 连字符）"},
                "data": None}
    if not body.description.strip():
        return {"ok": False,
                "error": {"code": "VALIDATION",
                          "message": "description 必填（fm.description.required 为 error 级门禁）"},
                "data": None}
    return await _submit(request, user_id, "skill_create", {
        "slug": body.slug, "name": body.name,
        "description": body.description, "author": body.author,
    })


@router.post("/{slug}/delete")
async def skill_delete(slug: str, body: SkillDeleteRequest, request: Request,
                       user_id: str = Depends(get_current_user_id)) -> dict:
    """删除 Skill（P3 CRUD）。默认 dry_run 只预览将消失的 canonical 路径
    （FR-1.0.26 ②），确认后带 confirm_removals 重发才执行。
    runtime 磁盘不动（红线 16）。动作用 POST 子路径（与 rollback 同风格，
    避免 DELETE + body 的客户端兼容问题）。"""
    if not slug or "/" in slug or ".." in slug or slug.startswith("."):
        return {"ok": False,
                "error": {"code": "VALIDATION", "message": "非法 slug"},
                "data": None}
    return await _submit(request, user_id, "skill_delete", {
        "slug": slug, "dry_run": body.dry_run,
        "confirm_removals": body.confirm_removals, "author": body.author,
    })
