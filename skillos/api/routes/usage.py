"""用量视图（N-01.7 / N-01.8 / N-11.7）：`GET /api/usage/summary`、`GET /api/skills/{slug}/usage`。

两个都是**本地数据**（事件表 + 日聚合 + 落库 decay 行），经 daemon 具名 op 取：
- `usage_summary`：Top 10 / 零调用 / 僵尸（僵尸口径唯一实现 `usage_service.zombie_slugs`）；
- `usage_events`：单 skill 近 N 次调用 + session 调用链。

云端另有一套 `/api/daemon/usage/summary`（跨设备团队总量，N-01.6）——
那是别的口径，别把两者混着用。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Query as FastAPIQuery

from skillos.api.deps import get_current_user_id
from skillos.api.routes._daemon_proxy import call_daemon

router = APIRouter(tags=["usage"])

_MAX_DAYS = 180
_MAX_EVENTS = 500


def _validate_slug(slug: str) -> str:
    if not slug or "/" in slug or ".." in slug or slug.startswith("."):
        raise HTTPException(status_code=422, detail="VALIDATION")
    return slug


@router.get("/usage/summary")
async def usage_summary(
    request: Request,
    days: int = FastAPIQuery(30, description="Top / 零调用窗口（天，1~180）"),
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """管理者视图榜单（N-01.8）。僵尸固定按 90 天零调用 + staleness=degraded（N-01.7）。"""
    span = max(1, min(days, _MAX_DAYS))
    return await call_daemon(request, user_id, "usage_summary", {"days": span})


@router.get("/skills/{slug}/usage")
async def skill_usage(
    slug: str,
    request: Request,
    limit: int = FastAPIQuery(100, description="近 N 次调用（≤500）"),
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """Skill 详情「调用记录」tab（N-11.7）：事件 30 天滚动清理，早于此无记录属正常。"""
    _validate_slug(slug)
    span = max(1, min(limit, _MAX_EVENTS))
    return await call_daemon(
        request, user_id, "usage_events", {"slug": slug, "limit": span},
    )
