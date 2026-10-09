"""daemon 日志查看代理（FR-1.1.12，04 §6.2.5）。

云端不碰磁盘（红线 5）；日志文件在 daemon 本机的 `~/.skillos/logs/daemon.log`。
前端轮询 → 云端路由 → hub.submit_task → daemon 端 `logs` 任务 → 读本机文件 → 返 JSON。
任务表即白名单（OUT-14）：`logs` 是新加的具名领域操作，不暴露任意文件读取。

约束（与现有 routes 一致）：
- 不在云端落文件盘（容器部署无 fs/log 限制）
- daemon 离线 → 503 DAEMON_OFFLINE（与 file_read 同模式）
- 路径 / 字段白名单化：仅允许读 `~/.skillos/logs/<mode>.log`，过滤参数由
  云端转交；不允许传 `path` 之类的任意文件路径（避免任意文件读）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query as FastAPIQuery, Request

from skillos.api.deps import get_current_user_id
from skillos.core.constants import WS_TASK_OPS

router = APIRouter(prefix="/logs", tags=["logs"])

_ALLOWED_MODES: frozenset[str] = frozenset({"daemon", "api", "mcp"})
_LEVELS: frozenset[str] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)
_MAX_TAIL: int = 5000  # 防滥用；前端 5s 轮询一次即可


@router.get("")
async def list_logs(
    request: Request,
    mode: str = FastAPIQuery("daemon", description="daemon / api / mcp"),
    tail: int = FastAPIQuery(100, description="末尾 N 行（≤ 5000）"),
    level: str = FastAPIQuery("DEBUG", description="最低级别"),
    op_id: str | None = FastAPIQuery(None, description="按 op_id 过滤"),
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """拉取本地 daemon 日志（FR-1.1.12）。

    任务经 daemon → daemon latency 校验白名单 `WS_TASK_OPS`（包含 `logs`）。
    """
    if mode not in _ALLOWED_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"未知 mode: {mode}（可选 {_ALLOWED_MODES}）",
        )
    if level.upper() not in _LEVELS:
        raise HTTPException(
            status_code=422,
            detail=f"未知 level: {level}（可选 {_LEVELS}）",
        )
    tail = max(0, min(tail, _MAX_TAIL))
    try:
        # 任务名按白名单（OUT-14）：logs 是新加具名 op，不暴露任意文件读取
        _assert_op_whitelisted("logs")
        result = await request.app.state.hub.submit_task(
            user_id, "logs",
            {"mode": mode, "tail": tail, "level": level.upper(), "op_id": op_id},
            timeout=15.0,
        )
    except ConnectionError:
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE") from None
    if not result.get("ok"):
        err = (result.get("error") or {})
        code = err.get("code") or "INTERNAL"
        raise HTTPException(status_code=502, detail=code)
    return result.get("data") or {"events": [], "skipped": 0}


def _assert_op_whitelisted(op: str) -> None:
    """任务表即白名单（OUT-14）：构造期 + 运行期双保险。"""
    if op not in WS_TASK_OPS:
        raise HTTPException(status_code=500, detail=f"非白名单操作: {op}")