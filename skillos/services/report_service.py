"""N-02 体检报告（`docs/12` §5.5）：只读聚合 + 落库，不改任何状态。

三个设计要点：

1. **报告是快照，不是实时视图**。要回答"自上次以来新增了什么"（N-02.2），
   必须有上一期的基线；基线只能存在上一份报告的 payload 里 —— 没有第二张历史表。
2. **计算在锁外**（N-02.5 / T17）。`generate()` 只读既有表（漂移矩阵 + `skill_checks`），
   落库是一次短事务；重活不占写锁，不阻塞正常任务。
3. **无变化也要留一份**（N-02.6），`unchanged=True` 单独成列 —— 周期连续可查，
   而不是"这周没报告"让人怀疑任务挂了。

口径单点：健康分复用 `core/algorithm/checks.summarize_health`（E-01.1/2），
折旧状态复用落库的 `skill_checks(scope=decay)` 行（E-02.6），漂移态复用 `DRIFT_SEVERITY`。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from skillos.core.algorithm.checks import summarize_health
from skillos.core.constants import DRIFT_SEVERITY
from skillos.core.logging import get_logger

log = get_logger("skillos.report")

# N-02.2 ①：报告只关心这三种"变坏"的漂移态（SYNCED / OUTDATED / UNMANAGED 不报增量）
REPORT_STATES: tuple[str, ...] = ("CONFLICT", "MISSING", "MODIFIED")
# N-02.2 边界：明细截断到 Top 10（payload 不随 skill 数膨胀）
TOP_N = 10
# N-02.2 ④：健康度降幅 Top 5
SCORE_DROP_N = 5
# N-02.1：首次生成没有上一期时，按默认周报窗口回看
DEFAULT_PERIOD_DAYS = 7

_LEVEL_TO_STATUS = {"info": "ok", "warn": "degraded", "unknown": "unknown"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(at: datetime | None) -> str | None:
    return at.isoformat() if at is not None else None


class ReportService:
    """体检报告编排层。

    `snapshot()` 由 engine 注入，返回**已归一化**的当前状态：
    `{"states": [{slug, runtime_key, state}],`
    `"checks": [{slug, scope, rule, level, message, value}]}`
    —— 归一化放在 services 层，判定/口径仍在 core（分层单向）。
    """

    def __init__(
        self,
        store: Any,
        snapshot: Callable[[], dict],
        *,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._snapshot = snapshot
        self._now = now

    # ---------------------------------------------------------------- 生成

    def generate(
        self,
        *,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> dict:
        """生成并落库一份报告，返回报告字典（含 payload）。"""
        end = period_end or self._now()
        previous = self._store.list_health_reports(limit=1)
        prev = previous[0] if previous else None
        start = period_start or (prev.period_end if prev is not None else
                                 end - timedelta(days=DEFAULT_PERIOD_DAYS))
        prev_baseline = _load_baseline(prev)

        current = _analyze(self._snapshot())
        payload = _build_payload(current, prev_baseline, start=start, end=end)

        report_id = self._store.add_health_report(
            period_start=start, period_end=end,
            payload=payload, unchanged=payload["unchanged"],
        )
        log.info(
            "report_generated",
            report_id=report_id,
            unchanged=payload["unchanged"],
            new_drift=payload["drift"]["new_count"],
            new_gate_errors=payload["gate"]["new_count"],
            newly_degraded=payload["decay"]["count"],
        )
        return {"id": report_id, **payload}

    # ---------------------------------------------------------------- 读取

    def list_reports(self, limit: int = 20) -> list[dict]:
        """列表摘要：**不含基线**（基线是给下一期算 diff 的，前端用不上）。"""
        return [_summary(row) for row in self._store.list_health_reports(limit=limit)]

    def get_report(self, report_id: str) -> dict | None:
        row = self._store.get_health_report(report_id)
        if row is None:
            return None
        payload = _load_payload(row)
        payload.pop("baseline", None)
        return {"id": row.id, "unchanged": bool(row.unchanged),
                "period_start": _iso(row.period_start), "period_end": _iso(row.period_end),
                "created_at": _iso(row.created_at), **payload}


# ---------------------------------------------------------------- 内部


def _load_payload(row: Any) -> dict:
    try:
        data = json.loads(row.payload)
    except (TypeError, ValueError):  # 历史行损坏：报错不猜（不静默给空报告）
        raise ValueError(f"报告 payload 不是合法 JSON: {row.id}") from None
    return data if isinstance(data, dict) else {}


def _load_baseline(row: Any | None) -> dict:
    if row is None:
        return {}
    base = _load_payload(row).get("baseline")
    return base if isinstance(base, dict) else {}


def _summary(row: Any) -> dict:
    payload = _load_payload(row)
    drift = payload.get("drift") or {}
    gate = payload.get("gate") or {}
    decay = payload.get("decay") or {}
    health = payload.get("health") or {}
    return {
        "id": row.id,
        "period_start": _iso(row.period_start),
        "period_end": _iso(row.period_end),
        "created_at": _iso(row.created_at),
        "unchanged": bool(row.unchanged),
        "new_drift": drift.get("new_count", 0),
        "new_gate_errors": gate.get("new_count", 0),
        "newly_degraded": decay.get("count", 0),
        "avg_health": health.get("avg"),
    }


def _analyze(snapshot: dict) -> dict:
    """把快照归一成「集合 + 计数 + 健康分」，diff 只在集合上做。"""
    states: set[tuple[str, str, str]] = set()
    counts: dict[str, int] = {state: 0 for state in REPORT_STATES}
    for item in snapshot.get("states") or []:
        slug = str(item.get("slug") or "")
        runtime_key = str(item.get("runtime_key") or "")
        state = str(item.get("state") or "")
        if not slug or not state:
            continue
        states.add((slug, runtime_key, state))
        if state in counts:
            counts[state] += 1

    errors: set[tuple[str, str]] = set()
    decay: dict[str, dict[str, str]] = {}
    decay_value: dict[str, dict] = {}
    for row in snapshot.get("checks") or []:
        slug = str(row.get("slug") or "")
        if not slug:
            continue
        if row.get("scope") == "gate":
            if row.get("level") == "error":
                errors.add((slug, str(row.get("rule") or "")))
            continue
        if row.get("scope") == "decay":
            decay.setdefault(slug, {})[str(row.get("rule") or "")] = str(row.get("level") or "")
            raw = row.get("value")
            if raw:
                try:
                    decay_value.setdefault(slug, json.loads(raw))
                except (TypeError, ValueError):
                    pass   # value 只用于下钻展示；解析不了不影响状态口径

    scores: dict[str, int] = {}
    for slug, dims in decay.items():
        summary = summarize_health([], {"value": decay_value.get(slug, {}), **{
            _status_key(dim): _LEVEL_TO_STATUS.get(level, "unknown")
            for dim, level in dims.items()
        }})
        scores[slug] = int(summary["score"])

    degraded = sorted(slug for slug, dims in decay.items()
                      if dims.get("staleness") == "warn")
    return {
        "states": states, "counts": counts, "errors": errors,
        "degraded": degraded, "scores": scores,
        # 空仓库等原因导致漂移不可测：原因透传给展示层，不吞掉
        "drift_unavailable": snapshot.get("drift_unavailable"),
    }


def _status_key(dimension: str) -> str:
    """`summarize_health` 优先读 `<dim>_status`；依赖维度与旧字段同名，故加后缀。"""
    return f"{dimension}_status"


def _build_payload(current: dict, prev: dict, *, start: datetime, end: datetime) -> dict:
    prev_states = {tuple(x) for x in (prev.get("states") or []) if len(x) == 3}
    prev_counts: dict[str, int] = {state: 0 for state in REPORT_STATES}
    for _slug, _rt, state in prev_states:
        if state in prev_counts:
            prev_counts[state] += 1

    # 只报"变坏"的三态（N-02.2 ①）：SYNCED / OUTDATED / UNMANAGED 进报告只是噪音
    new_states = {
        (s, r, st) for s, r, st in (current["states"] - prev_states)
        if st in REPORT_STATES
    }
    new_drift = sorted(
        ({"slug": s, "runtime_key": r, "state": st} for s, r, st in new_states),
        key=lambda d: (-DRIFT_SEVERITY.get(d["state"], 0), d["slug"], d["runtime_key"]),
    )

    prev_errors = {tuple(x) for x in (prev.get("errors") or []) if len(x) == 2}
    new_errors = sorted(current["errors"] - prev_errors)

    prev_degraded = set(prev.get("degraded") or [])
    newly_degraded = [s for s in current["degraded"] if s not in prev_degraded]

    prev_scores = {
        str(k): int(v) for k, v in (prev.get("scores") or {}).items()
        if isinstance(v, (int, float))
    }
    drops = [
        {"slug": slug, "from": prev_scores[slug], "to": score,
         "delta": score - prev_scores[slug]}
        for slug, score in current["scores"].items()
        if slug in prev_scores and score < prev_scores[slug]
    ]
    drops.sort(key=lambda d: (d["delta"], d["slug"]))

    scores_now = sorted(current["scores"].values())
    avg = round(sum(scores_now) / len(scores_now), 1) if scores_now else None

    counts = {
        state: {
            "now": current["counts"].get(state, 0),
            "prev": prev_counts.get(state, 0),
            "delta": current["counts"].get(state, 0) - prev_counts.get(state, 0),
        }
        for state in REPORT_STATES
    }

    unchanged = not (new_drift or new_errors or newly_degraded or drops)

    return {
        "period": {"start": _iso(start), "end": _iso(end)},
        "unchanged": unchanged,
        "drift": {
            "counts": counts,
            "new_count": len(new_drift),
            "new": new_drift[:TOP_N],          # N-02.2 边界：明细截断
            # None = 漂移正常统计；有值 = 这次没法统计（如空仓库），原因直出
            "unavailable": current.get("drift_unavailable"),
        },
        "gate": {
            "errors_now": len(current["errors"]),
            "new_count": len(new_errors),
            "new": [{"slug": s, "rule": r} for s, r in new_errors[:TOP_N]],
        },
        "decay": {
            "degraded_now": len(current["degraded"]),
            "count": len(newly_degraded),
            "newly_degraded": newly_degraded[:TOP_N],
        },
        "health": {"avg": avg, "drops": drops[:SCORE_DROP_N]},
        # 下一期算 diff 用的基线（不展示；规模与 skill 数线性，几十 KB 以内）
        "baseline": {
            "states": sorted(list(s) for s in current["states"]),
            "errors": sorted(list(e) for e in current["errors"]),
            "degraded": current["degraded"],
            "scores": current["scores"],
        },
    }
