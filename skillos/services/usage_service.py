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


class _Switch(Protocol):
    def collect_enabled(self) -> bool: ...


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
