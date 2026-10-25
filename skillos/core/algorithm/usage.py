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

from skillos.core.constants import USAGE_REPORT_FIELDS, USAGE_REPORT_MAX_ROWS


class UsageEventLike(Protocol):
    """事件表行的最小契约（避免 core 依赖 store.models）。"""

    at: datetime
    skill_id: str
    via: str
    op: str
    duration_ms: int | None
    tokens_in: int
    tokens_out: int


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
        # token 来自事件行（L1 事件恒 0，L2 会话适配器回填估算值）；不在这里估算
        tokens_in = sum(getattr(e, "tokens_in", 0) or 0 for e in group)
        tokens_out = sum(getattr(e, "tokens_out", 0) or 0 for e in group)
        rows.append({
            "device_id": device_id,
            "skill_id": skill_id,
            "date": day,
            "via": via,
            "count": len(group),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "duration_ms_p50": percentile(durations, 0.50),
            "duration_ms_p95": percentile(durations, 0.95),
            "by_op": json.dumps(dict(sorted(ops.items())), ensure_ascii=False),
        })
    return rows


# ---------------------------------------------------------------- 上报行清洗（N-01.5）

_BY_OP_MAX_CHARS: int = 4096   # by_op 是 JSON 小字典；超长视为异常报文，不落库


def sanitize_report_rows(rows: object, *, device_id: str) -> tuple[list[dict], int]:
    """云端对上行上报行的**第二道隐私防线**（NFR-V2-03 / T16 同思路：不信自报）。

    只保留 `USAGE_REPORT_FIELDS` 里的字段，`device_id` 一律用认证态覆盖 ——
    设备自报的 id 可伪造，也不该决定数据进哪个桶。

    返回 `(干净行, 被丢弃行数)`：
    - **整批**形态错误（非数组 / 超行数上限）→ 抛 `ValueError`，让调用方回错误信封。
      这里是「协议错误」，静默清洗会把"报文变形"伪装成"今天没数据"。
    - **单行**不可解析（缺 skill/via、日期非法、计数为负）→ 丢弃并计入 skipped，
      由 ack 回报，不静默吞掉。
    """
    if rows is None:
        return [], 0
    if not isinstance(rows, list):
        raise ValueError("rows 必须是数组")
    if len(rows) > USAGE_REPORT_MAX_ROWS:
        raise ValueError(f"rows 超过上限 {USAGE_REPORT_MAX_ROWS}")

    clean: list[dict] = []
    skipped = 0
    for row in rows:
        item = _sanitize_row(row, device_id=device_id)
        if item is None:
            skipped += 1
            continue
        clean.append(item)
    return clean, skipped


def _sanitize_row(row: object, *, device_id: str) -> dict | None:
    if not isinstance(row, dict):
        return None
    skill_id = row.get("skill_id")
    via = row.get("via")
    day = row.get("date")
    if not isinstance(skill_id, str) or not skill_id.strip():
        return None
    if not isinstance(via, str) or not via.strip():
        return None
    if isinstance(day, str):
        try:
            day = date.fromisoformat(day)
        except ValueError:
            return None
    if not isinstance(day, date) or isinstance(day, datetime):
        return None

    count = _non_negative_int(row.get("count"))
    tokens_in = _non_negative_int(row.get("tokens_in"))
    tokens_out = _non_negative_int(row.get("tokens_out"))
    if count is None or tokens_in is None or tokens_out is None:
        return None
    by_op = row.get("by_op")
    if by_op is not None and (not isinstance(by_op, str) or len(by_op) > _BY_OP_MAX_CHARS):
        return None

    item = {
        "device_id": device_id,          # 认证态盖章，不采信报文里的值
        "skill_id": skill_id.strip(),
        "date": day,
        "via": via.strip(),
        "count": count,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "duration_ms_p50": _optional_int(row.get("duration_ms_p50")),
        "duration_ms_p95": _optional_int(row.get("duration_ms_p95")),
        "by_op": by_op,
    }
    # 出口再按白名单过滤一次：将来设备侧多构造字段也不会"自动"上云
    return {k: v for k, v in item.items() if k in USAGE_REPORT_FIELDS}


def _non_negative_int(value: object) -> int | None:
    """非负整数；非法/负数返回 None（由调用方判整行无效，不做 0 兜底）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None
