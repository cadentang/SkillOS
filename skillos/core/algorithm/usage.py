"""调用遥测日聚合（N-11.3 / TQ-02）：纯函数，输入事件、输出日桶。

为什么放 core：聚合是**派生计算**，可重放；把它做成纯函数，`usage_aggregate` 定时任务
就能随时「以事件表重算」，不依赖任何增量状态（TQ-02 决议）。
桶键 `(date, skill_id, via)` 与 `skill_usage_daily` 的唯一约束一一对应。
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from typing import Protocol


class UsageEventLike(Protocol):
    """事件表行的最小契约（避免 core 依赖 store.models）。"""

    at: datetime
    skill_id: str
    via: str
    op: str
    duration_ms: int | None


def percentile(values: Sequence[int], q: float) -> int | None:
    """最近秩法分位数（不做线性插值）。

    分位数只用于展示「快不快」，口径固定比算法先进更重要 —— 换算法会让历史曲线跳变。
    """
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


def bucket_date(at: datetime) -> date:
    """聚合日统一按 UTC 归日（本地时只影响「什么时候跑」，不影响「算到哪一天」）。"""
    return (at.astimezone(UTC) if at.tzinfo else at.replace(tzinfo=UTC)).date()


def aggregate_buckets(
    events: Iterable[UsageEventLike],
    *,
    device_id: str = "local",
) -> list[dict]:
    """把事件流聚成日桶行（顺序稳定：按 date / skill_id / via 排序）。"""
    grouped: dict[tuple[date, str, str], list[UsageEventLike]] = {}
    for event in events:
        key = (bucket_date(event.at), event.skill_id, event.via)
        grouped.setdefault(key, []).append(event)

    rows: list[dict] = []
    for (day, skill_id, via), group in sorted(grouped.items()):
        durations = [
            e.duration_ms for e in group
            if isinstance(e.duration_ms, int) and not isinstance(e.duration_ms, bool)
        ]
        ops = Counter(e.op for e in group)
        rows.append({
            "device_id": device_id,
            "skill_id": skill_id,
            "date": day,
            "via": via,
            "count": len(group),
            # L2 未接：token 缺省 0（不猜、不估算）
            "tokens_in": 0,
            "tokens_out": 0,
            "duration_ms_p50": percentile(durations, 0.50),
            "duration_ms_p95": percentile(durations, 0.95),
            "by_op": json.dumps(dict(sorted(ops.items())), ensure_ascii=False),
        })
    return rows
