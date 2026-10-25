"""体检报告（N-02.3 / N-02.6）：`GET /api/reports`、`GET /api/reports/{id}`。

报告是**本地表**（`health_reports`），由 daemon 定时任务或 CLI 生成；
云端只代理读取列表/详情，不生成也不缓存（红线 5/6）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi import Query as FastAPIQuery

from skillos.api.deps import get_current_user_id
from skillos.api.routes._daemon_proxy import call_daemon

router = APIRouter(prefix="/reports", tags=["reports"])

_MAX_LIMIT = 100


@router.get("")
async def list_reports(
    request: Request,
    limit: int = FastAPIQuery(20, description="最近 N 份（≤100）"),
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """报告列表（摘要：期间 / 是否无变化 / 三项增量计数 / 平均健康度）。"""
    span = max(1, min(limit, _MAX_LIMIT))
    return await call_daemon(request, user_id, "report_list", {"limit": span},
                             timeout=15.0)


@router.get("/{report_id}")
async def get_report(report_id: str, request: Request,
                     user_id: str = Depends(get_current_user_id)) -> dict:
    """报告详情（payload 全文，不含下一期基线）。"""
    return await call_daemon(request, user_id, "report_get", {"id": report_id},
                             timeout=15.0)
