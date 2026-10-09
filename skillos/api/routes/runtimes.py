"""Runtime 设置（FR-2.4.10 / T26 / FR-1.0.32~34 / T24~25）：

- GET  /api/runtimes/discovered      经 daemon task `runtimes_list` 扫白名单 + 4 builtin
                                      云端合并 custom Runtime（来自 Runtime 表）
- POST /api/runtimes/custom          云端直接写 Runtime 表（custom: 命名空间）
- PATCH/DELETE /api/runtimes/{key}   同上，custom 条目专属
- GET  /api/runtimes/{key}/diagnose  经 daemon task `runtime_diagnose` 校验 path

写操作**只在 Web handler 注册**（T25 / T26 红线），MCP / 后台批量无调用点。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from skillos.api.deps import get_current_user_id, get_repo
from skillos.store.cloud_repo import CloudRepo

router = APIRouter(prefix="/runtimes", tags=["runtimes"])


# ---------------------------------------------------------------- Pydantic schemas


class CustomRuntimeCreate(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    name: str | None = None
    root_path: str = Field(min_length=1, max_length=4096)
    scope: str = "user"
    discover_only: bool = False  # T25：显式状态机，默认 False（已纳管）


class CustomRuntimeUpdate(BaseModel):
    root_path: str = Field(min_length=1, max_length=4096)
    discover_only: bool | None = None  # None = 不改


# ---------------------------------------------------------------- helpers


def _to_runtime_dict(rt) -> dict:
    """Runtime ORM → 统一 dict。custom 段渲染用。"""
    return {
        "key": rt.key,
        "name": rt.name or rt.key,
        "path": rt.root_path,
        "custom": True,
        "managed": not rt.discover_only,
        "discover_only": rt.discover_only,
        "enabled": rt.enabled,
        "scope": rt.scope,
        "created_at": rt.created_at.isoformat() if rt.created_at else None,
    }


async def _submit(request: Request, user_id: str, op: str, params: dict,
                  timeout: float = 30.0) -> dict:
    """统一任务下发：离线 503，result 信封透传。"""
    try:
        return await request.app.state.hub.submit_task(
            user_id, op, params, timeout=timeout,
        )
    except ConnectionError:
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE") from None


def _envelope_error(err: dict | None) -> dict:
    return {"code": (err or {}).get("code", "INTERNAL"),
            "message": (err or {}).get("message", "")}


# ---------------------------------------------------------------- 端点


@router.get("/discovered")
async def list_discovered(
    request: Request,
    force: bool = False,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    """未纳管 + 已纳管 Runtime 一次返回（FR-1.0.32 / T24 / T26）。

    - 经 daemon task `runtimes_list` 扫白名单 + 4 builtin（candidates / builtins）
    - 云端合并该 user 的 custom Runtime（来自 Runtime 表，discover_only=False 视为已纳管）
    - 前端按 `managed` 字段切两段
    """
    result = await _submit(request, user_id, "runtimes_list", {"force": force})
    if not result.get("ok"):
        return {"ok": False, "data": None,
                "error": _envelope_error(result.get("error"))}
    daemon_data = result.get("data") or {}

    # 合并 cloud 端 custom Runtime（已纳管段）
    custom_rts = repo.list_custom_runtimes(user_id)
    custom_items = [_to_runtime_dict(rt) for rt in custom_rts]

    # builtin 4 个标 managed=True / custom=False（统一字段）
    builtins = []
    for b in daemon_data.get("builtins") or []:
        builtins.append({
            "key": b["key"], "name": b.get("name") or b["key"],
            "version": b.get("version"),
            "custom": False, "managed": True, "discover_only": False,
        })

    # candidates 段：白名单命中但 key 还没纳管的（剔除已纳管 custom + builtin）。
    # custom:<slug> 纳管后，白名单同名候选不再重复显示（否则"加入"后
    # 已纳管 / 未纳管两段都有同一 Runtime —— bug：joinRuntime 生成
    # custom:cursor 而 managed_keys 只含 custom:cursor 不含 cursor）
    managed_keys = {b["key"] for b in builtins} | {c["key"] for c in custom_items}
    custom_slugs = {
        c["key"].split(":", 1)[1]
        for c in custom_items if ":" in c["key"]
    }
    candidates = [
        c for c in (daemon_data.get("candidates") or [])
        if c.get("key") and c["key"] not in managed_keys
        and c["key"] not in custom_slugs
    ]

    return {
        "ok": True,
        "data": {
            "scanned_at": daemon_data.get("scanned_at"),
            "home": daemon_data.get("home"),
            "managed": builtins + custom_items,
            "candidates": candidates,
        },
        "error": None,
    }


@router.post("/custom")
async def create_custom(
    body: CustomRuntimeCreate,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    """创建 custom Runtime 空条目（FR-1.0.34 / T25）。

    key 必须 `custom:<name>` 命名空间（FR-1.0.25），避免与 builtin 冲突。
    """
    if not body.key.startswith("custom:"):
        return {"ok": False, "data": None, "error": {
            "code": "VALIDATION",
            "message": "custom Runtime key 必须 'custom:' 命名空间（FR-1.0.25）",
        }}
    try:
        rt = repo.create_custom_runtime(
            key=body.key, name=body.name, root_path=body.root_path,
            scope=body.scope, owner_id=user_id,
            discover_only=body.discover_only,
        )
    except ValueError as e:
        return {"ok": False, "data": None, "error": {
            "code": "RUNTIME_EXISTS" if "已存在" in str(e) else "VALIDATION",
            "message": str(e),
        }}
    return {"ok": True, "data": _to_runtime_dict(rt), "error": None}


@router.patch("/{key:path}")
async def update_custom(
    key: str, body: CustomRuntimeUpdate,
    user_id: str = Depends(get_current_user_id),  # noqa: ARG001（保留：未来 user-scope 检查）
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    """编辑 custom Runtime 路径（FR-1.0.25 / T25）。

    discover_only 显式传：None = 不改；True/False = 改。T25 红线：不能"改路径就
    自动 discover_only=False"——用户必须显式确认。
    """
    rt = repo.update_custom_runtime_path(
        key, body.root_path, body.discover_only,
    )
    if rt is None:
        return {"ok": False, "data": None, "error": {
            "code": "NOT_FOUND",
            "message": f"custom Runtime 不存在: {key}",
        }}
    return {"ok": True, "data": _to_runtime_dict(rt), "error": None}


@router.delete("/{key:path}")
async def delete_custom(
    key: str,
    user_id: str = Depends(get_current_user_id),  # noqa: ARG001
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    """删除 custom Runtime（NFR-11.4 / T25）。

    仅 custom 条目可删（不删 builtin）。删除同时清理 binding。
    """
    ok = repo.delete_custom_runtime(key)
    if not ok:
        return {"ok": False, "data": None, "error": {
            "code": "NOT_FOUND",
            "message": f"custom Runtime 不存在或非 custom 条目: {key}",
        }}
    return {"ok": True, "data": {"key": key, "deleted": True}, "error": None}


@router.get("/{key:path}/diagnose")
async def diagnose_custom(
    key: str, request: Request,
    user_id: str = Depends(get_current_user_id),
    repo: CloudRepo = Depends(get_repo),
) -> dict:
    """Runtime 路径诊断（FR-1.0.33 / D9 / FR-2.4.10 已纳管行通用）。

    custom：云端查表拿 root_path → daemon task 校验；
    builtin / 白名单候选：传 key，daemon 端按 adapter / yaml 约定路径解析
    （云端不知道用户 home，不能自己拼路径 —— T1 root 显式注入原则）。
    """
    rt = repo.get_runtime_by_key(key)
    if rt is not None and rt.adapter_key == "custom":
        params = {"root_path": rt.root_path}
    else:
        params = {"key": key}
    result = await _submit(request, user_id, "runtime_diagnose",
                            params, timeout=15.0)
    if not result.get("ok"):
        return {"ok": False, "data": None,
                "error": _envelope_error(result.get("error"))}
    return {"ok": True, "data": result.get("data"), "error": None}
