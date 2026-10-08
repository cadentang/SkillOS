"""版本 / diff / 回滚 / tag / 文件级历史（FR-3.2 / P4 FR-2.2.8）。

P2：log 时间轴；P3：diff；P4：回滚（removals 守卫 FR-1.0.26 ②）、
tag、文件级历史。全部经 daemon 任务下发（本地 git 是唯一真值，云端无副本 NFR-1）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from skillos.api.deps import get_current_user_id

router = APIRouter(prefix="/skills", tags=["versions"])


class RollbackRequest(BaseModel):
    ref: str
    dry_run: bool = True
    confirm_removals: list[str] = []
    author: str = "web <web@skillos.local>"


class TagCreateRequest(BaseModel):
    name: str
    message: str = ""


def _validate_slug(slug: str) -> None:
    if not slug or "/" in slug or ".." in slug:
        raise HTTPException(status_code=422, detail="VALIDATION")


async def _submit(request: Request, user_id: str, op: str, params: dict) -> dict:
    """统一任务下发：离线 503，result 信封透传。"""
    try:
        result = await request.app.state.hub.submit_task(user_id, op, params, timeout=60.0)
    except ConnectionError:
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE") from None
    if not result.get("ok"):
        err = result.get("error") or {}
        payload = {
            "ok": False,
            "error": {"code": err.get("code", "INTERNAL"),
                      "message": err.get("message", "")},
            "removals": result.get("removals", []),
            "data": None,
        }
        return payload
    return {"ok": True, "data": result.get("data"), "error": None}


@router.get("/{slug}/log")
async def skill_log(slug: str, request: Request, limit: int = 50,
                    user_id: str = Depends(get_current_user_id)) -> dict:
    """溯源时间轴（FR-2.2.7 / FR-1.1.10）：数据直接来自 git log，不新增表。

    daemon 离线 → 503（时间轴依赖本地 git，云端无副本可读）。
    """
    _validate_slug(slug)
    return await _submit(request, user_id, "version_log",
                         {"slug": slug, "limit": min(limit, 200)})


@router.get("/{slug}/diff")
async def skill_diff(slug: str, request: Request, base: str, head: str,
                     user_id: str = Depends(get_current_user_id)) -> dict:
    """版本间 diff（FR-2.2.8 第一版）：unified 文本（T5 二进制只显示「已变更」）。"""
    _validate_slug(slug)
    return await _submit(request, user_id, "diff",
                         {"slug": slug, "base": base, "head": head})


@router.post("/{slug}/rollback")
async def skill_rollback(slug: str, body: RollbackRequest, request: Request,
                         user_id: str = Depends(get_current_user_id)) -> dict:
    """回滚（FR-2.2.8 / P4）。默认 dry_run 只预览将消失的路径（FR-1.0.26 ②），
    确认后带 confirm_removals 重发才执行。回滚 commit 即溯源（FR-1.0.22）。"""
    _validate_slug(slug)
    return await _submit(request, user_id, "rollback", {
        "slug": slug, "ref": body.ref, "dry_run": body.dry_run,
        "confirm_removals": body.confirm_removals, "author": body.author,
    })


@router.get("/{slug}/file-history")
async def skill_file_history(slug: str, request: Request, path: str, limit: int = 50,
                             user_id: str = Depends(get_current_user_id)) -> dict:
    """文件级历史（P4）：--follow 跨改名追踪单文件提交序列。"""
    _validate_slug(slug)
    if not path or ".." in path:
        raise HTTPException(status_code=422, detail="VALIDATION")
    return await _submit(request, user_id, "file_history",
                         {"slug": slug, "path": path, "limit": min(limit, 200)})


@router.get("/{slug}/tags")
async def skill_tags(slug: str, request: Request,
                     user_id: str = Depends(get_current_user_id)) -> dict:
    """tag 清单（时间轴徽标数据源；tag 为 repo 级，此处按 skill 路由仅作分组入口）。"""
    _validate_slug(slug)
    return await _submit(request, user_id, "tag_list", {})


@router.post("/{slug}/tags")
async def skill_tag_create(slug: str, body: TagCreateRequest, request: Request,
                           user_id: str = Depends(get_current_user_id)) -> dict:
    """在当前 HEAD 打附注 tag（P4）。"""
    _validate_slug(slug)
    return await _submit(request, user_id, "tag_create",
                         {"name": body.name, "message": body.message})
