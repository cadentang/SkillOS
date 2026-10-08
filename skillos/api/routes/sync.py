"""漂移查询、push/pull 下发（FR-3.3 / FR-3.4）。

- 云端只下发任务，不执行磁盘操作（红线 5）
- 文件内容不经云端缓存（FR-3.12）：file_read 一律下发任务实时取回
- 漂移矩阵按 Skill 聚合取最严重，含部分态（FR-2.1.9 / 07 N11）
- 离线降级（FR-2.4.4）：读接口返回缓存 + stale 标注；写接口 503 DAEMON_OFFLINE
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from skillos.api.deps import get_current_user_id, get_repo
from skillos.store.cloud_repo import (
    CloudRepo,
    aggregate_skill_status,
    drift_summary,
    manager_stats,
)

router = APIRouter(prefix="/sync", tags=["sync"])


def _matrix_rows(repo: CloudRepo, user_id: str,
                 live_cells: list[dict] | None = None) -> list[dict]:
    """矩阵行 = Skill（聚合态）；cells = {runtime_key: {state, 徽标}}。

    live_cells 非空时（daemon 在线实时取）带徽标；缓存视图无徽标（T6 派生字段不落库）。
    """
    pairs = repo.list_bindings(user_id)
    badge: dict[tuple[str, str], dict] = {}
    live_by_skill: dict[str, dict[str, dict]] = {}
    for cell in live_cells or []:
        key = (str(cell.get("slug") or ""), str(cell.get("runtime_key") or ""))
        badge[key] = {
            "has_untracked_assets": bool(cell.get("has_untracked_assets")),
            "adapter_outdated": bool(cell.get("adapter_outdated")),
        }
        live_by_skill.setdefault(key[0], {})[key[1]] = {
            "state": str(cell.get("state") or "").upper(),
            **badge[key],
        }
    rows: list[dict] = []
    for slug, cells in (
        live_by_skill.items() if live_cells else
        _cached_cells(pairs).items()
    ):
        states = [c["state"] for c in cells.values()]
        rows.append({
            "slug": slug,
            "state": aggregate_skill_status([s.lower() for s in states]),
            "cells": cells,
        })
    rows.sort(key=lambda r: r["slug"])
    return rows


def _cached_cells(pairs) -> dict[str, dict[str, dict]]:
    """云端缓存 → {slug: {runtime_key: {state}}}（无徽标）。"""
    out: dict[str, dict[str, dict]] = {}
    for skill, binding, runtime in pairs:
        out.setdefault(skill.slug, {})[runtime.key] = {"state": binding.status.upper()}
    return out


async def _live_matrix(request: Request, user_id: str) -> list[dict] | None:
    """daemon 在线时取实时矩阵（含徽标）；离线返回 None（调用方降级缓存）。"""
    try:
        # 90s：daemon 首次全扫（27 skill × git 子进程）在 Windows 上 25s+，
        # 30s 太贴线——超时会被误判离线降级缓存（表现即"页面慢且数据旧"）
        result = await request.app.state.hub.submit_task(
            user_id, "status", {}, timeout=90.0,
        )
    except ConnectionError:
        return None
    if not result.get("ok"):
        return None
    return (result.get("data") or {}).get("matrix") or []


@router.get("/status")
async def drift_status(request: Request, live: bool = True,
                       user_id: str = Depends(get_current_user_id),
                       repo: CloudRepo = Depends(get_repo)) -> dict:
    """漂移矩阵 + 双读者统计（Dashboard 数据源）。

    live=true（默认）：daemon 在线则实时取矩阵（含徽标）并回写云端缓存；
    离线读保留（FR-2.4.4）：返回云端缓存 + stale=true。
    """
    cells = await _live_matrix(request, user_id) if live else None
    if cells is not None:
        repo.upsert_matrix(user_id, cells)
    pairs = repo.list_bindings(user_id)
    rows = _matrix_rows(repo, user_id, live_cells=cells)
    return {
        "ok": True,
        "data": {
            "rows": rows,
            "summary": drift_summary(pairs) if cells is None else _row_summary(rows),
            "manager": manager_stats(pairs),
            "daemon_online": cells is not None,
            "stale": cells is None,
        },
        "error": None,
    }


def _row_summary(rows: list[dict]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for r in rows:
        summary[r["state"]] = summary.get(r["state"], 0) + 1
    return summary


class PushIn(BaseModel):
    skill_ids: list[str] = Field(min_length=1)
    runtime_keys: list[str] | None = None
    on_conflict: str = "abort"  # 仅 Web 人工路径可达 force（FR-1.3.5）
    dry_run: bool = False
    # RemovalsConfirmDialog 确认后的删除路径清单（FR-1.0.26 / FR-2.4.7）
    confirm_removals: list[str] | None = None


@router.post("/push")
async def push(body: PushIn, request: Request,
               user_id: str = Depends(get_current_user_id),
               repo: CloudRepo = Depends(get_repo)) -> dict:
    """下发 push 任务到在线 daemon。离线 → 503 DAEMON_OFFLINE（前端进离线态）。

    错误信封（FR-3.19）：REMOVALS_REQUIRE_CONFIRM 附 held_back_removals[]，
    Web 弹 RemovalsConfirmDialog；确认后带 confirm_removals 重发。
    """
    if body.on_conflict not in ("abort", "skip", "force"):
        raise HTTPException(status_code=422, detail="VALIDATION")
    hub = request.app.state.hub
    try:
        result = await hub.submit_task(
            user_id, "push",
            {
                "skill_ids": body.skill_ids,
                "runtime_keys": body.runtime_keys,
                "on_conflict": body.on_conflict,
                "dry_run": body.dry_run,
                "confirm_removals": body.confirm_removals,
            },
            timeout=180.0,
        )
    except ConnectionError:
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE") from None
    ok = bool(result.get("ok"))
    data = result.get("data") or {}
    # 审计：push 结果落 sync_records（无论成败，NFR-9）
    if not body.dry_run:
        for slug in body.skill_ids:
            skill = repo.get_skill_by_slug(slug)
            repo.record_sync(
                "push",
                skill.id if skill else None,
                None,
                {"ok": ok, "results": data.get("results"), "via": "web"},
                by=user_id,
            )
    if not ok:
        err = result.get("error") or {}
        return {"ok": False,
                "error": {"code": err.get("code", "INTERNAL"),
                          "message": err.get("message", ""),
                          "held_back_removals": err.get("held_back_removals", [])},
                "data": data}
    return {"ok": True, "data": data, "error": None}


@router.post("/gate-summary")
async def gate_summary(request: Request,
                       user_id: str = Depends(get_current_user_id)) -> dict:
    """门禁计数（ManagerPanel）：每 skill 的 error / warn 数（daemon 实时算）。"""
    try:
        result = await request.app.state.hub.submit_task(
            user_id, "gate_summary", {}, timeout=60.0,
        )
    except ConnectionError:
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE") from None
    if not result.get("ok"):
        err = result.get("error") or {}
        return {"ok": False,
                "error": {"code": err.get("code", "INTERNAL"),
                          "message": err.get("message", "")},
                "data": None}
    return {"ok": True, "data": result.get("data"), "error": None}
