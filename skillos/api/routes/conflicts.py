"""CONFLICT 四选一 API（FR-2.2.6 / T22 / RED LINE 12）。

仅 Web 人工路径可达，daemon 任务分发到本地引擎（MCP / CLI 不暴露 keep_both）。
冲突解决走 WS 任务 → 本地 resolve_conflict()，结果经 result 信封透传。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from skillos.api.deps import get_current_user_id

router = APIRouter(prefix="/skills", tags=["conflicts"])


_VALID_DECISIONS: frozenset[str] = frozenset(
    {"keep_local", "keep_remote", "keep_both", "defer"}
)


class ResolveConflictBody(BaseModel):
    decision: str = Field(
        description="四选一：keep_local / keep_remote / keep_both / defer",
    )
    confirmed_removals: list[str] = Field(default_factory=list)
    author: str = "web <web@skillos.local>"


def _validate_slug(slug: str) -> None:
    if not slug or "/" in slug or ".." in slug:
        raise HTTPException(status_code=422, detail="VALIDATION")


def _validate_runtime_key(runtime_key: str) -> None:
    if not runtime_key or "/" in runtime_key or ".." in runtime_key:
        raise HTTPException(status_code=422, detail="VALIDATION")


async def _submit(request: Request, user_id: str, op: str, params: dict) -> dict:
    try:
        result = await request.app.state.hub.submit_task(
            user_id, op, params, timeout=60.0,
        )
    except ConnectionError:
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE") from None
    if not result.get("ok"):
        err = result.get("error") or {}
        return {
            "ok": False,
            "error": {"code": err.get("code", "INTERNAL"),
                      "message": err.get("message", "")},
            "data": None,
        }
    return {"ok": True, "data": result.get("data"), "error": None}


@router.post("/{slug}/conflicts/{runtime_key}/resolve")
async def resolve_conflict(
    slug: str,
    runtime_key: str,
    body: ResolveConflictBody,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """CONFLICT 四选一（FR-2.2.6 / RED LINE 12 / T22）。

    仅 Web 人工路径。决策前自动安全快照（决策可回滚）。
    keep_both 选项只在 Web 可见（MCP 工具签名故意不暴露，test_conflicts_p3 红线守护）。
    """
    _validate_slug(slug)
    _validate_runtime_key(runtime_key)
    if body.decision not in _VALID_DECISIONS:
        raise HTTPException(
            status_code=422,
            detail=f"非法决策: {body.decision!r}（仅 {_VALID_DECISIONS}）",
        )
    return await _submit(request, user_id, "resolve_conflict", {
        "slug": slug,
        "runtime_key": runtime_key,
        "decision": body.decision,
        "confirmed_removals": body.confirmed_removals,
        "author": body.author,
    })