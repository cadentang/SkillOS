"""文件树 / 内容读取 / 编辑保存（FR-3.2 / FR-2.2.2 / FR-2.2.3）。

- 云端只存文件树与哈希，不缓存文件内容（FR-3.12）：read 一律下发任务实时取回
- 写走 base_hash 乐观锁：不匹配 → 409 STALE，不覆盖（FR-2.4.5 / FR-3.13）
- 路径穿越 → 400 PATH_TRAVERSAL，不回显解析后的绝对路径
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from skillos.api.deps import get_current_user_id

router = APIRouter(prefix="/skills", tags=["files"])


def _validate_slug(slug: str) -> str:
    if not slug or "/" in slug or ".." in slug or slug.startswith("."):
        raise HTTPException(status_code=422, detail="VALIDATION")
    return slug


@router.get("/{slug}/files")
async def file_tree(slug: str, request: Request,
                    user_id: str = Depends(get_current_user_id)) -> dict:
    """canonical 文件树索引（路径 + sha256 + 大小）。"""
    _validate_slug(slug)
    try:
        result = await request.app.state.hub.submit_task(
            user_id, "file_tree", {"slug": slug}, timeout=30.0,
        )
    except ConnectionError:
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE") from None
    if not result.get("ok"):
        err = result.get("error") or {}
        return {"ok": False,
                "error": {"code": err.get("code", "INTERNAL"),
                          "message": err.get("message", "")},
                "data": None}
    return {"ok": True, "data": result.get("data"), "error": None}


@router.get("/{slug}/files/content")
async def file_read(slug: str, request: Request, path: str,
                    user_id: str = Depends(get_current_user_id)) -> dict:
    """单文件内容（经 daemon 实时取回，附 base_hash 供编辑器乐观锁）。"""
    _validate_slug(slug)
    try:
        result = await request.app.state.hub.submit_task(
            user_id, "file_read", {"slug": slug, "path": path}, timeout=30.0,
        )
    except ConnectionError:
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE") from None
    if not result.get("ok"):
        err = result.get("error") or {}
        code = str(err.get("code", "INTERNAL"))
        status = 400 if code == "PATH_TRAVERSAL" else 404
        raise HTTPException(status_code=status, detail=code)
    return {"ok": True, "data": result.get("data"), "error": None}


class FileWriteIn(BaseModel):
    path: str = Field(min_length=1)
    content: str
    base_hash: str  # 乐观锁：GET files/content 返回的 hash（FR-3.13）
    message: str = "web 编辑"


@router.post("/{slug}/files")
async def file_write(slug: str, body: FileWriteIn, request: Request,
                     user_id: str = Depends(get_current_user_id)) -> dict:
    """编辑保存：STALE → 409（附当前 hash，前端提示重新加载，不覆盖）。"""
    _validate_slug(slug)
    try:
        result = await request.app.state.hub.submit_task(
            user_id, "file_write",
            {
                "slug": slug, "path": body.path, "content": body.content,
                "base_hash": body.base_hash, "message": body.message,
            },
            timeout=60.0,
        )
    except ConnectionError:
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE") from None
    if not result.get("ok"):
        err = result.get("error") or {}
        code = str(err.get("code", "INTERNAL"))
        status = {"STALE": 409, "PATH_TRAVERSAL": 400}.get(code, 500)
        # STALE 响应不携带内容（防止旧数据覆盖，FR-3.13）
        raise HTTPException(status_code=status, detail={"code": code,
                                                        "message": err.get("message", "")})
    return {"ok": True, "data": result.get("data"), "error": None}
