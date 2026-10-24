"""设备侧开关读写（V2-T1，键值约定见 `docs/12` §3）。

为什么单独一层：`usage_service.record()` 在**每个工具调用**上都要问一次「采集开不开」，
直接查库会把热路径变慢（5.1 要求「每 30s 刷新一次开关缓存」）。缓存放在这里，
其他模块只调 `collect_enabled()` / `report_enabled()`，不自己读表。

默认值就是产品决策（`docs/12` §3）：采集 **on**、上报 **off**、报告 weekly。
读不到的键一律给默认值 —— 这是**设计约定**而非"报错兜底"：开关表第一次用时本来就是空的。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol

# 键 → 默认值（与 docs/12 §3 表逐行对齐；新增键必须同时更新那张表）
DEFAULTS: dict[str, Any] = {
    "usage_collect_enabled": True,     # N-11.8 / Q-04：默认采集
    "usage_report_enabled": False,     # N-01.5 / Q-04：默认不上报（隐私优先）
    "telemetry.runtime_paths": {},     # N-11.9
    "report.cadence": "weekly",        # N-02.1
    "upstream.check_enabled": True,    # E-08.1
}

_COLLECT_KEY = "usage_collect_enabled"
_REPORT_KEY = "usage_report_enabled"
_PATHS_KEY = "telemetry.runtime_paths"


class _SettingsStore(Protocol):
    def get_setting(self, key: str) -> str | None: ...
    def set_setting(self, key: str, value: str) -> None: ...


class SettingsService:
    """KV 读写 + 开关缓存。`value` 一律 JSON 序列化（布尔 / 对象同一条路）。"""

    def __init__(
        self,
        store: _SettingsStore,
        *,
        cache_ttl_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._ttl = cache_ttl_s
        self._clock = clock
        self._cache: dict[str, tuple[Any, float]] = {}

    # ---------------------------------------------------------------- 基础读写

    def get(self, key: str) -> Any:
        if key not in DEFAULTS:
            # 未知键不猜默认值：调用方要么写错键名，要么该键还没进约定表
            raise KeyError(f"未登记的开关键: {key}（先更新 docs/12 §3 的关键键值约定表）")
        cached = self._cache.get(key)
        if cached is not None:
            value, expires_at = cached
            if self._clock() < expires_at:
                return value
        raw = self._store.get_setting(key)
        value = DEFAULTS[key] if raw is None else json.loads(raw)
        self._cache[key] = (value, self._clock() + self._ttl)
        return value

    def set(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            raise KeyError(f"未登记的开关键: {key}（先更新 docs/12 §3 的关键键值约定表）")
        self._store.set_setting(key, json.dumps(value, ensure_ascii=False))
        # 自己写完立刻生效，不等 TTL —— 否则用户点了开关要等半分钟才有反应
        self._cache[key] = (value, self._clock() + self._ttl)

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)

    # ---------------------------------------------------------------- 具名开关

    def collect_enabled(self) -> bool:
        return bool(self.get(_COLLECT_KEY))

    def report_enabled(self) -> bool:
        return bool(self.get(_REPORT_KEY))

    def runtime_paths(self) -> dict[str, list[str]]:
        value = self.get(_PATHS_KEY)
        return value if isinstance(value, dict) else {}

    def set_runtime_paths(self, runtime_key: str, paths: list[str]) -> None:
        """L2 手动路径（N-11.9）：设置即覆盖；空列表 = 清除该 Runtime 的手动路径。"""
        current = dict(self.runtime_paths())
        if paths:
            current[runtime_key] = list(paths)
        else:
            current.pop(runtime_key, None)
        self.set(_PATHS_KEY, current)
