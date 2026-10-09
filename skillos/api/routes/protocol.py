"""协议版本状态（FR-1.0.29 / T18 / FR-2.4.10 Settings 协议区块）。

GET /api/protocol/status —— 云端代理 daemon WS task `protocol_status`。

设计决策（**不抛 503**，仿 `daemon.py` `/api/daemon/status` 风格）:
- daemon 离线 → envelope `{ok: True, data: {ok: False, reason: "daemon offline", ...}}`
- 前端 `data.ok` 三态判断（一致 / 异常 / 离线）
- 抛 503 会让 `client.ts:request` 抛 ApiError → `useQuery` data=undefined →
  前端 "data is undefined" —— 上一版就撞这个坑.

任务表即白名单（OUT-14）：`protocol_status` 是 P3 新加的具名 op，已在
`core/constants.py` `WS_TASK_OPS` 中。构造期 + 运行期双保险。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from skillos.api.deps import get_current_user_id
from skillos.core.constants import WS_TASK_OPS

router = APIRouter(prefix="/protocol", tags=["protocol"])


def _offline_data(reason: str) -> dict:
    """daemon 不可用时的占位 data —— 字段结构与 `engine.protocol_status()` 一致.

    前端用 `data.ok` 三态判断:
    - ok=True: 协议一致
    - ok=False, reason=daemon offline: daemon 没连, 数据不可用
    - ok=False, reason=其他: 协议本身不一致
    """
    return {
        "current_supported": None,
        "sticky_version": None,
        "trailer_version": None,
        "head_message_first_line": None,
        "ok": False,
        "read_only": True,
        "reason": reason,
    }


def _envelope(data: dict) -> dict:
    """仿 `daemon.py` 风格 envelope —— 前端 client.ts:31 解构."""
    return {"ok": True, "data": data, "error": None}


@router.get("/status")
async def protocol_status(
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """返回协议状态（FR-1.0.29 / T18 / FR-2.4.10 Settings 协议区块）.

    返回字段（`engine.protocol_status()` 直接透传, 离线时用 `_offline_data`）:
    - current_supported: int | None
    - sticky_version: int | None
    - trailer_version: int | None
    - head_message_first_line: str | None
    - ok: bool（协议一致性 + daemon 可用性合并）
    - read_only: bool
    - reason: str | None
    """
    hub = request.app.state.hub

    # 仿 daemon.py: 先查 hub 在线, 不抛 503
    if not hub.agent_online(user_id):
        return _envelope(_offline_data("daemon offline"))

    # daemon 在线 → 走 hub.submit_task 拿真实数据
    if "protocol_status" not in WS_TASK_OPS:
        # 构造期 + 运行期双保险（OUT-14）
        raise HTTPException(
            status_code=500, detail="非白名单操作: protocol_status"
        )
    try:
        result = await request.app.state.hub.submit_task(
            user_id, "protocol_status", {}, timeout=15.0,
        )
    except ConnectionError:
        # 在线但 task 提交失败（极端: daemon 中途断）→ 仍返回结构化
        return _envelope(_offline_data("daemon task submit failed"))
    if not result.get("ok"):
        err = result.get("error") or {}
        return _envelope(
            _offline_data(f"daemon op failed: {err.get('code', 'INTERNAL')}")
        )
    # 正常路径 —— 透传 engine.protocol_status() 的 8 字段
    return _envelope(result.get("data") or {})
