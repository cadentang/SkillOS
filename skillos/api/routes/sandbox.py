"""沙盒 API（FR-3.10 / FR-2.4.3，P7）。

- `GET /api/health` 的 sandbox 字段由 create_app 设置（SKILLOS_SANDBOX=1）
- `POST /api/sandbox/reset`：仅沙盒模式存在（否则 404），经 hub 下发
  `sandbox_reset` 任务给沙盒 daemon —— 云端不碰磁盘（红线 5），
  文件恢复全部在容器内 daemon 侧执行
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from skillos.api.deps import get_current_user_id

router = APIRouter(tags=["sandbox"])


@router.post("/sandbox/reset")
async def sandbox_reset(request: Request,
                        user_id: str = Depends(get_current_user_id)) -> dict:
    """恢复预置演示数据（评委可反复体验的前提）。

    非沙盒部署此路由不存在（create_app 不挂载）；沙盒下 daemon 离线 → 503。
    """
    if not getattr(request.app.state, "sandbox", False):
        raise HTTPException(status_code=404, detail="NOT_SANDBOX")
    try:
        result = await request.app.state.hub.submit_task(
            user_id, "sandbox_reset", {}, timeout=120.0,
        )
    except ConnectionError:
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE") from None
    if not result.get("ok"):
        err = result.get("error") or {}
        raise HTTPException(status_code=500, detail=err.get("message", "RESET_FAILED"))
    return {"ok": True, "data": result.get("data"), "error": None}
