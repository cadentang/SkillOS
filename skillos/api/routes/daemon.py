"""daemon 状态（FR-2.4.10 Settings "守护状态" 区块）与用量上报（N-01.4/6/9）。

GET  /api/daemon/status        —— 云端直接读 hub（不需 task，离线时也能用）
POST /api/daemon/usage/sync    —— 向设备拉一次聚合行并合入云端（N-01.4）
GET  /api/daemon/usage/summary —— 跨设备按 skill 汇总（N-01.6，只见总量）

上报是**拉取式**的：云端用 WS 任务通道的具名 op `usage_report` 向在线 daemon 要数，
设备侧开关（`usage_report_enabled`，默认 off）关闭时 daemon 直接拒绝 —— 于是「零上报」
在协议层就能被证明，而不是靠"没人调用"。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from skillos.api.deps import get_current_user_id, get_repo
from skillos.store.cloud_repo import CloudRepo

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


@router.post("/usage/sync")
async def usage_sync(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    """向在线 daemon 拉一次聚合行并合入云端（N-01.4/9）。

    合并是**覆盖**而非累加：设备每次上报近 30 天全量，重连补报不会翻倍。
    daemon 侧开关关闭时返回 `REPORT_DISABLED`，这里原样透出（不伪造成功）。
    """
    hub = request.app.state.hub
    try:
        conn, result = await hub.submit_task_with_conn(user_id, "usage_report", {})
    except ConnectionError:
        return {"ok": False, "error": {
            "code": "DAEMON_OFFLINE", "message": "daemon 不在线，用量数据暂无法拉取",
        }, "data": None}

    if not result.get("ok"):
        return {
            "ok": False,
            "error": result.get("error") or {"code": "IO", "message": "上报失败"},
            "data": None,
        }
    rows = (result.get("data") or {}).get("rows") or []
    # device_id 由云端按**应答连接**盖章（= token_id，每设备唯一）：设备自报值一律不用，
    # 否则两台机器会在 (device_id, skill_id, date, via) 上互相覆盖
    for row in rows:
        row["device_id"] = conn.device_id
    merged = repo.merge_usage_daily(rows)
    return {"ok": True, "data": {"merged": merged, "device_id": conn.device_id},
            "error": None}


@router.get("/usage/summary")
async def usage_summary(
    days: int = 30,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    """跨设备按 skill 汇总（N-01.6：只有总调用次数，不暴露个人明细）。

    租户边界靠「该 user 名下设备的 **token_id** 集合」限定（= 落库的 device_id）——
    `skill_usage_daily` 没有 user_id 列，不按设备过滤会把其他账号的调用次数加进来。
    """
    span = max(1, min(days, 180))
    device_ids = [d.id for d in repo.list_devices(user_id)]
    items = repo.usage_summary(days=span, device_ids=device_ids)
    return {"ok": True, "data": {"days": span, "items": items}, "error": None}
