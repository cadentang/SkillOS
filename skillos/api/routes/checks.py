"""质量检查（FR-3.5 / FR-1.0.11 / NFR-10.5）。

- 判定唯一入口在 core/algorithm/checks.run_gate（daemon 与 push 共用）
- 结果落库 skill_checks（云端，NFR-9 审计）；error 级在 push 侧拦截（FR-1.0.11）
- 折旧度量（FR-1.0.23）：staleness / dependency_broken 纯静态分析
- 健康度（FR-1.0.24 退化形态）：门禁结果计数 + 折旧原始值，可下钻（NFR-10.3）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from skillos.api.deps import get_current_user_id, get_repo
from skillos.core.algorithm.checks import summarize_health
from skillos.services.check_service import decay_rows
from skillos.store.cloud_repo import CloudRepo

router = APIRouter(prefix="/skills", tags=["checks"])


def _validate_slug(slug: str) -> str:
    if not slug or "/" in slug or ".." in slug or slug.startswith("."):
        raise HTTPException(status_code=422, detail="VALIDATION")
    return slug


@router.get("/{slug}/checks")
async def skill_checks(slug: str, request: Request,
                       user_id: str = Depends(get_current_user_id),
                       repo: CloudRepo = Depends(get_repo)) -> dict:
    """逐条门禁 findings 并落库 skill_checks（含折旧度量）。"""
    _validate_slug(slug)
    try:
        gate = await request.app.state.hub.submit_task(
            user_id, "check", {"slug": slug}, timeout=30.0,
        )
        decay = await request.app.state.hub.submit_task(
            user_id, "decay", {"slug": slug}, timeout=30.0,
        )
    except ConnectionError:
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE") from None
    if not gate.get("ok"):
        err = gate.get("error") or {}
        return {"ok": False,
                "error": {"code": err.get("code", "INTERNAL"),
                          "message": err.get("message", "")},
                "data": None}

    findings = (gate.get("data") or {}).get("findings") or []
    decay_data = (decay.get("data") or {}) if decay.get("ok") else None
    # 落库（NFR-9）：逐条 findings + 折旧三维（E-02.6，同表不同 scope，互不覆盖）
    skill = repo.get_skill_by_slug(slug)
    if skill is not None:
        repo.record_checks(skill.id, findings)
        if decay_data:
            repo.record_decay_checks(skill.id, decay_rows(decay_data))
    # 健康度（FR-1.0.24 退化形态：门禁结果计数，T11 —— 仅展示，禁入放行判定）
    health = summarize_health(findings, decay_data)
    return {
        "ok": True,
        "data": {
            "has_error": (gate.get("data") or {}).get("has_error", False),
            "findings": findings,
            "decay": decay_data,
            "health": health,
        },
        "error": None,
    }
