"""V2-M2-03：app_settings 开关读写与缓存（V2-T1 / docs/12 §3）。"""

from __future__ import annotations

import pytest

from skillos.services.settings_service import DEFAULTS, SettingsService
from skillos.store.db import init_schema, make_engine, make_session_factory
from skillos.store.index_repo import SqliteIndexStore


def _store() -> SqliteIndexStore:
    engine = make_engine("sqlite://")
    init_schema(engine)
    return SqliteIndexStore(make_session_factory(engine)())


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestDefaults:
    def test_defaults_match_documented_decisions(self) -> None:
        """Q-04 的产品决策：默认采集、默认不上报。"""
        svc = SettingsService(_store())
        assert svc.collect_enabled() is True
        assert svc.report_enabled() is False
        assert svc.runtime_paths() == {}
        assert svc.get("report.cadence") == "weekly"
        assert svc.get("upstream.check_enabled") is True

    def test_unknown_key_raises(self) -> None:
        """未登记键直接报错 —— 猜默认值会让打错键名的开关永远静默失效。"""
        svc = SettingsService(_store())
        with pytest.raises(KeyError):
            svc.get("usage_collect_enable")   # 少一个 d

    def test_all_defaults_are_registered(self) -> None:
        svc = SettingsService(_store())
        for key in DEFAULTS:
            svc.get(key)   # 不抛即说明约定表自洽


class TestRoundTrip:
    def test_bool_and_object_roundtrip(self) -> None:
        store = _store()
        svc = SettingsService(store)
        svc.set("usage_report_enabled", True)
        svc.set("telemetry.runtime_paths", {"claude-code": ["D:/cc"]})

        fresh = SettingsService(store)   # 新实例 = 无缓存，读的是库里的值
        assert fresh.report_enabled() is True
        assert fresh.runtime_paths() == {"claude-code": ["D:/cc"]}

    def test_set_runtime_paths_merges_and_clears(self) -> None:
        svc = SettingsService(_store())
        svc.set_runtime_paths("claude-code", ["D:/a"])
        svc.set_runtime_paths("codex", ["D:/b"])
        assert set(svc.runtime_paths()) == {"claude-code", "codex"}
        svc.set_runtime_paths("claude-code", [])
        assert set(svc.runtime_paths()) == {"codex"}


class TestCache:
    def test_cache_serves_hot_path_until_ttl(self) -> None:
        """5.1：每 30s 刷新一次开关缓存，不能每个事件都查库。"""
        store = _store()
        clock = _Clock()
        svc = SettingsService(store, cache_ttl_s=30.0, clock=clock)
        assert svc.collect_enabled() is True

        store.set_setting("usage_collect_enabled", "false")   # 绕过 svc 直接改库
        assert svc.collect_enabled() is True                  # 仍在 TTL 内 → 旧值

        clock.advance(31)
        assert svc.collect_enabled() is False                 # TTL 到期 → 重新读库

    def test_write_takes_effect_immediately(self) -> None:
        """用户点开关不该等 30 秒才生效。"""
        svc = SettingsService(_store())
        assert svc.collect_enabled() is True
        svc.set("usage_collect_enabled", False)
        assert svc.collect_enabled() is False
