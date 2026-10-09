"""协议版本状态（FR-1.0.29 / T18 / FR-2.4.10 Settings 协议区块）。

GET /api/protocol/status —— 云端代理 daemon WS task `protocol_status`。
daemon 离线 → 503 DAEMON_OFFLINE（与 file_read / logs 同模式）。

任务表即白名单（OUT-14）：`protocol_status` 是 P3 新加的具名 op，已在
`core/constants.py` `WS_TASK_OPS` 中。构造期 + 运行期双保险（与 logs.py
的 `_assert_op_whitelisted` 同思路）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from skillos.api.deps import get_current_user_id
from skillos.core.constants import WS_TASK_OPS

router = APIRouter(prefix="/protocol", tags=["protocol"])


@router.get("/status")
async def protocol_status(
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """返回 sticky + HEAD trailer + 一致性（FR-1.0.29 / T18）。

    返回字段与 `engine.protocol_status()` 一致：
    - current_supported: int（本机支持的最大协议版本）
    - sticky_version: int | None（仓库 `.skillos/protocol.json` 锁定的版本）
    - trailer_version: int | None（HEAD commit trailer 中的版本）
    - head_message_first_line: str | None
    - ok: bool（sticky / trailer / current 一致且 >= current_supported）
    - read_only: bool（仅读，禁止写）
    - reason: str | None（不一致原因）
    """
    if "protocol_status" not in WS_TASK_OPS:
        # 构造期 + 运行期双保险
        raise HTTPException(
            status_code=500, detail="非白名单操作: protocol_status"
        )
    try:
        result = await request.app.state.hub.submit_task(
            user_id, "protocol_status", {}, timeout=15.0,
        )
    except ConnectionError:
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE") from None
    if not result.get("ok"):
        err = result.get("error") or {}
        raise HTTPException(
            status_code=502, detail=err.get("code", "INTERNAL")
        )
    return result.get("data") or {}
