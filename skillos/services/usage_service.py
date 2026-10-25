"""调用遥测采集（N-01.1/2 / N-11.2，`docs/12` §5.1）。

**唯一写事件表的入口**（V2-T2）：engine / MCP / WS 处理器 / CLI 只调 `record()`，
谁也不许自己 insert —— 采集口径一分散，Dashboard 的「零调用」就再也对不上了。

三条硬约束：
- 开关 `usage_collect_enabled=false` 时**直接 return**（零开销路径，开关值有 30s 缓存）；
- 没有 skill 归属的调用不记（Dashboard 浏览、全库 scan 属 N-01.1 排除项）；
- `param_digest` 只能是摘要，正文与参数值永不入表（N-11.6 隐私边界）。

聚合**不在写路径做**：事件表是事实源，日聚合由 daemon 定时以事件表重算（TQ-02）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol

from skillos.core.algorithm.usage import aggregate_buckets
from skillos.core.constants import STALENESS_DEGRADED_DAYS
from skillos.core.logging import get_logger

log = get_logger("skillos.usage")

# via 取值域（N-01.2）。daemon 内部动作照收，展示层过滤（决策 2026-10-24）
VIA_MCP = "mcp"
VIA_WEB = "web"
VIA_CLI = "cli"
VIA_DAEMON = "daemon"
VIA_VALUES = frozenset({VIA_MCP, VIA_WEB, VIA_CLI, VIA_DAEMON})

# N-11.1：事件表保留 30 天；聚合窗口与它对齐（5.2：近 30 天重算）
EVENT_RETENTION_DAYS = 30
AGGREGATE_WINDOW_DAYS = 30
# N-01.9：断连补偿靠「每次把窗口内聚合全量重报 + 服务端去重合并」，
# 所以上报窗口与聚合窗口同宽，漏报的日期下次自然补齐
REPORT_WINDOW_DAYS = 30


def new_session_id() -> str:
    """会话标识：MCP / WS 每连接一个，CLI 每进程一个（跨进程会话聚合交给 L2）。"""
    return f"s_{secrets.token_hex(8)}"


def param_digest(**params: Any) -> str:
    """参数摘要（只存哈希，不存值）——N-11.6：参数与正文不上云、不落表。"""
    payload = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class _UsageStore(Protocol):
    def add_usage_event(self, *, session_id: str, runtime: str, skill_id: str, op: str,
                        via: str, duration_ms: int | None, ok: bool,
                        param_digest: str | None,
                        tokens_in: int = 0, tokens_out: int = 0) -> None: ...
    def list_usage_events(self, since: datetime) -> list[Any]: ...
    def replace_usage_daily(self, since: Any, rows: list[dict]) -> int: ...
    def purge_usage_events(self, before: datetime) -> int: ...
    def list_usage_daily(self, since: Any) -> list[Any]: ...
    def list_skill_events(self, skill_id: str, limit: int = 100) -> list[Any]: ...
    def list_events_by_sessions(self, session_ids: list[str],
                                per_session: int = 50) -> list[Any]: ...


class _Switch(Protocol):
    def collect_enabled(self) -> bool: ...
    def report_enabled(self) -> bool: ...


class UsageService:
    """L1 采集：只写事件表，不做聚合。"""

    def __init__(self, store: _UsageStore, settings: _Switch, *,
                 session_id: str | None = None) -> None:
        self._store = store
        self._settings = settings
        self.session_id = session_id or new_session_id()

    def record(
        self,
        skill_id: str | None,
        op: str,
        *,
        via: str,
        duration_ms: int | None = None,
        ok: bool = True,
        runtime: str = "",
        session_id: str | None = None,
        digest: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> bool:
        """记一次调用。返回是否真的落库（关开关 / 无归属 / 非法 via 都返回 False）。

        返回 bool 是为了让测试能断言「关开关零新增行」，而不是靠查库间接证明。
        """
        if not skill_id:
            return False                       # 无 skill 归属：N-01.1 排除项
        if via not in VIA_VALUES:
            log.warning("usage_record_rejected", reason="bad_via", via=via)
            return False
        if not self._settings.collect_enabled():
            return False                       # 开关关闭：直接返回，零开销（5.1）
        self._store.add_usage_event(
            session_id=session_id or self.session_id,
            runtime=runtime,
            skill_id=str(skill_id),
            op=op,
            via=via,
            duration_ms=duration_ms,
            ok=ok,
            param_digest=digest,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        return True

    # ---------------------------------------------------------------- 聚合 / 清理

    def aggregate(self, *, days: int = AGGREGATE_WINDOW_DAYS,
                  now: datetime | None = None) -> int:
        """以事件表重算最近 `days` 天的日聚合（幂等，TQ-02）。

        幂等靠「窗口内整体覆盖」而不是累加：同一批事件跑两次结果必须一致，
        否则定时任务重试一次就把数字算翻倍。
        """
        current = now or datetime.now(UTC)
        since_day = (current - timedelta(days=days - 1)).date()
        since_dt = datetime.combine(since_day, time.min, tzinfo=UTC)
        events = self._store.list_usage_events(since_dt)
        return self._store.replace_usage_daily(since_day, aggregate_buckets(events))

    def purge(self, *, keep_days: int = EVENT_RETENTION_DAYS,
              now: datetime | None = None) -> int:
        """清理 30 天前事件。**只在聚合完成后调用**（5.2 表注：先重算后清理）。"""
        cutoff = (now or datetime.now(UTC)) - timedelta(days=keep_days)
        return self._store.purge_usage_events(cutoff)

    # ---------------------------------------------------------------- 云端上报（N-01.4/5）

    def report_rows(self, *, device_id: str = "local", days: int = REPORT_WINDOW_DAYS,
                    now: datetime | None = None) -> list[dict]:
        """构造上报报文：**只有聚合行**（N-01.5 明文允许的字段集）。

        开关关闭时返回空列表 —— 让「零上报」成为可断言的返回值，而不是靠"没人调用"。
        报文里绝不出现事件行、`param_digest`、Runtime 路径（N-11.6 / N-11.9⑤）。
        `device_id` 只是占位：云端一律用认证连接的 token_id 覆盖（设备自报值不可信）。
        """
        if not self._settings.report_enabled():
            return []
        current = now or datetime.now(UTC)
        since = (current - timedelta(days=days - 1)).date()
        rows = self._store.list_usage_daily(since)
        return [
            {
                "device_id": device_id,
                "skill_id": row.skill_id,
                "date": row.date.isoformat(),
                "via": row.via,
                "count": row.count,
                "tokens_in": row.tokens_in,
                "tokens_out": row.tokens_out,
                "duration_ms_p50": row.duration_ms_p50,
                "duration_ms_p95": row.duration_ms_p95,
                "by_op": row.by_op,
            }
            for row in rows
        ]

    # -------------------------------------------------- 管理者视图（N-01.7/8 / N-11.7）

    def totals_by_skill(self, *, days: int = AGGREGATE_WINDOW_DAYS,
                        now: datetime | None = None) -> dict[str, dict]:
        """窗口内按 skill 汇总：`{slug: {count, tokens_in, tokens_out}}`。

        只含"被调用过"的 skill —— 零调用集合要拿 slug 全集去减，
        不要在这里补 0：补 0 会让"没装这个 skill"和"装了但没用"混成一件事。
        """
        current = now or datetime.now(UTC)
        since = (current - timedelta(days=days - 1)).date()
        out: dict[str, dict] = {}
        for row in self._store.list_usage_daily(since):
            item = out.setdefault(row.skill_id, {
                "skill_id": row.skill_id, "count": 0, "tokens_in": 0, "tokens_out": 0,
            })
            item["count"] += int(row.count or 0)
            item["tokens_in"] += int(row.tokens_in or 0)
            item["tokens_out"] += int(row.tokens_out or 0)
        return out

    def top_called(self, *, slugs: list[str] | None = None,
                   days: int = REPORT_WINDOW_DAYS, limit: int = 10,
                   now: datetime | None = None) -> list[dict]:
        """近 N 天调用 Top（N-01.8）。`slugs` 非空时只统计这些 skill。"""
        totals = self.totals_by_skill(days=days, now=now)
        allowed = set(slugs) if slugs is not None else None
        rows = [v for k, v in totals.items() if allowed is None or k in allowed]
        rows.sort(key=lambda r: (-r["count"], r["skill_id"]))
        return rows[: max(1, limit)]

    def zero_called(self, *, slugs: list[str],
                    days: int = REPORT_WINDOW_DAYS,
                    now: datetime | None = None) -> list[str]:
        """窗口内零调用（N-01.8 第二个榜单）：`slugs` 全集减去有调用记录的。"""
        called = set(self.totals_by_skill(days=days, now=now))
        return sorted(set(slugs) - called)

    def zombie_slugs(self, *, slugs: list[str], degraded: set[str],
                     days: int = STALENESS_DEGRADED_DAYS,
                     now: datetime | None = None) -> list[str]:
        """僵尸口径**唯一实现**（N-01.7，用户 2026-10-24 拍板）：

        `近 days 天零调用` ∩ `staleness=degraded`，双条件缺一不可。
        旧口径（仅 staleness）不保留 —— 管理者视图只能走这一个函数，
        换口径 = 改这里一处，前端只负责把「实测 + 折旧」标注画出来。
        """
        called = set(self.totals_by_skill(days=days, now=now))
        return sorted(set(degraded) & (set(slugs) - called))

    def events_for(self, slug: str, *, limit: int = 100) -> list[dict]:
        """单 skill 近 N 次调用（N-11.7）：只有时间 / op / 耗时 / 结果 / token。

        不带 `param_digest`（参数摘要属隐私面，前端不需要它）。
        """
        return [
            {
                "at": e.at.isoformat(), "op": e.op, "via": e.via,
                "runtime": e.runtime, "session_id": e.session_id,
                "duration_ms": e.duration_ms, "ok": bool(e.ok),
                "tokens_in": e.tokens_in, "tokens_out": e.tokens_out,
            }
            for e in self._store.list_skill_events(slug, limit=limit)
        ]

    def session_chains(self, slug: str, *, limit: int = 100,
                       per_session: int = 50) -> list[dict]:
        """按 session 还原调用链（N-11.7 L3）：`[A → B → error]` 的顺序视图。

        事件表 30 天滚动清理，所以链只覆盖还能查到的事件 —— 不给"已清理"假数据。
        """
        events = self._store.list_skill_events(slug, limit=limit)
        session_ids = list(dict.fromkeys(e.session_id for e in events))
        if not session_ids:
            return []
        grouped: dict[str, list[dict]] = {sid: [] for sid in session_ids}
        for e in self._store.list_events_by_sessions(session_ids, per_session=per_session):
            bucket = grouped.get(e.session_id)
            if bucket is None or len(bucket) >= per_session:
                continue
            bucket.append({
                "at": e.at.isoformat(), "slug": e.skill_id, "op": e.op,
                "ok": bool(e.ok), "duration_ms": e.duration_ms,
            })
        return [
            {"session_id": sid, "steps": steps}
            for sid, steps in grouped.items() if steps
        ]
