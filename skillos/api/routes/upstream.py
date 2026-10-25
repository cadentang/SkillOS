"""上游来源与检查（FR-3.6 / E-08）。

上游事实唯一权威在 daemon 本地 `upstream_sources` 表，云端只代理（红线 5）。

- `POST /api/skills/{slug}/upstream/check` —— 单 skill「立即检查」（E-08.1）
- `POST /api/upstream/check` —— 全量检查（日任务的等价手动入口）

写 capability 不在此暴露：登记/修改上游源属于 M5 之后的范围，当前只做"检查"这一件事。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from skillos.api.deps import get_current_user_id

router = APIRouter(tags=["upstream"])


async def _submit(request: Request, user_id: str, params: dict) -> dict:
    try:
        result = await request.app.state.hub.submit_task(
            user_id, "upstream_check", params, timeout=120.0,
        )
    except ConnectionError:
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE") from None
    if not result.get("ok"):
        err = result.get("error") or {}
        return {"ok": False, "data": None,
                "error": {"code": err.get("code", "IO"),
                          "message": err.get("message", "")}}
    return {"ok": True, "data": result.get("data"), "error": None}


@router.post("/skills/{slug}/upstream/check")
async def upstream_check_skill(slug: str, request: Request,
                               user_id: str = Depends(get_current_user_id)) -> dict:
    """单 skill 上游检查（E-08.1 手动入口）：网络 IO 在 daemon 侧，云端只转发。"""
    if not slug or "/" in slug or ".." in slug:
        raise HTTPException(status_code=422, detail="INVALID_SLUG")
    return await _submit(request, user_id, {"slug": slug})


@router.post("/upstream/check")
async def upstream_check_all(request: Request,
                             user_id: str = Depends(get_current_user_id)) -> dict:
    """全量检查（与 daemon 日任务同一实现，`UpstreamService.check_all`）。"""
    return await _submit(request, user_id, {})
