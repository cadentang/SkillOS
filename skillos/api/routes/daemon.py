"""daemon 状态（FR-2.4.10 Settings "守护状态" 区块）。

GET /api/daemon/status —— 云端直接读 hub（不需 task，离线时也能用）。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from skillos.api.deps import get_current_user_id

router = APIRouter(prefix="/daemon", tags=["daemon"])


@router.get("/status")
async def daemon_status(
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """返回该 user 的 daemon 连接 / 在线数 / agents 列表（去敏感字段）。"""
    hub = request.app.state.hub
    online = hub.agent_online(user_id)
    now = time.time()
    agents: list[dict] = []
    for a in hub.agents.values():
        if a.user_id != user_id:
            continue
        agents.append({
            "agent_id": a.agent_id,
            "token_id": a.token_id,
            "connected_at": a.connected_at,
            "uptime_s": round(now - a.connected_at, 1) if online else 0,
        })
    return {
        "ok": True,
        "data": {
            "online": online,
            "agents": agents,
            "agent_count": len(agents),
        },
        "error": None,
    }
