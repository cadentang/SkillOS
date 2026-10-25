"""规则面板与相似检测（M3-05 / M4-04：N-03 / N-08 / E-04 / N-07）。

GET  /api/rules                        —— 生效规则 + 无效规则（含原因）+ 白名单 + ruleset_hash
GET  /api/similarity                   —— 相似对 + 合并建议 + 已弃用清单（只读数）
POST /api/similarity/run               —— 立即比对（覆盖式落库，幂等）
POST /api/similarity/{a}/{b}/resolve   —— 回收落选方（N-07.3，两段式确认）

数据全在 daemon 本地（规则文件与 `skill_similarity` 表），云端只代理（红线 5）。
所有 op 都在 `WS_TASK_OPS` 白名单里 —— 没有通用透传（OUT-14）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from skillos.api.deps import get_current_user_id

router = APIRouter(tags=["rules"])


class ResolveBody(BaseModel):
    """回收落选方（N-07.3）。

    - `keep` 必须是这一对里的一个；另一个即 `drop`（服务端推导，不让前端自己算）；
    - `dry_run=true`（默认）只列 removals，不碰磁盘；
    - 真跑必须把 `removals` 原样回传 `confirm_removals`（逐条确认，红线 16）。
    """

    keep: str
    dry_run: bool = True
    confirm_removals: list[str] = Field(default_factory=list)


async def _submit(request: Request, user_id: str, op: str,
                  params: dict | None = None) -> dict:
    """统一形状：daemon 离线 / 拒答都回显式错误码，不伪装成空数据。"""
    hub = request.app.state.hub
    try:
        result = await hub.submit_task(user_id, op, params or {})
    except ConnectionError:
        return {"ok": False, "data": None, "error": {
            "code": "DAEMON_OFFLINE", "message": "daemon 不在线，本地规则与相似数据不可用",
        }}
    if not result.get("ok"):
        return {"ok": False, "data": None,
                "error": result.get("error") or {"code": "IO", "message": "读取失败"}}
    return {"ok": True, "data": result.get("data"), "error": None}


@router.get("/rules")
async def rules_list(
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """规则面板（M3-05）：`rules` / `invalid` / `ignores` / `ruleset_hash` 四块。"""
    return await _submit(request, user_id, "rules_list")


@router.get("/similarity")
async def similarity_list(
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """相似对（≥0.75 提示；≥0.9 带 `suggestion`，N-07.1）。"""
    return await _submit(request, user_id, "similarity_list")


@router.post("/similarity/run")
async def similarity_run(
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """立即比对（TQ-03）：覆盖式写 `skill_similarity`，同输入重复调用不新增行。"""
    return await _submit(request, user_id, "similarity_run")


@router.post("/similarity/{a}/{b}/resolve")
async def similarity_resolve(
    a: str, b: str, body: ResolveBody, request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """回收落选方（N-07.3）：**不删 canonical**，只回收 runtime 副本 + 打弃用标记。

    两段式：先 `dry_run` 拿 removals 清单给用户看，确认后带 `confirm_removals`
    再调一次。N-07.4 的 sync_records 由 daemon 侧落（`engine.deprecate_skill`）。
    """
    if body.keep not in (a, b):
        raise HTTPException(status_code=422, detail={
            "code": "INVALID_KEEP", "message": "keep 必须是这一对里的某一个",
            "pair": [a, b],
        })
    drop = b if body.keep == a else a
    return await _submit(request, user_id, "skill_deprecate", {
        "slug": drop,
        "dry_run": body.dry_run,
        "confirm_removals": body.confirm_removals,
    })
