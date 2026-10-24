"""V2-M2-02：L1 采集收口（N-01 / N-11.2，docs/12 §5.1）。

三层断言：
1. `UsageService.record()` 是唯一写口，开关 / 无归属 / 非法 via 一律不落库；
2. daemon 侧（Web 入口）只在 skill 级 op 上计数，仪表盘类 op 不计 —— 否则「零调用」语义失真；
3. 关开关后 **零新增行**（GWT 原文），且要能立刻生效。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from skillos.daemon.main import TaskExecutor
from skillos.services.engine import EnvPaths, LocalEngine
from skillos.services.usage_service import (
    VIA_CLI,
    VIA_MCP,
    UsageService,
    new_session_id,
    param_digest,
)
from skillos.store.db import init_schema, make_engine, make_session_factory
from skillos.store.index_repo import SqliteIndexStore
from skillos.store.models import SkillUsageEvent


class _SwitchOn:
    def collect_enabled(self) -> bool:
        return True


class _SwitchOff:
    def collect_enabled(self) -> bool:
        return False


def _store() -> SqliteIndexStore:
    engine = make_engine("sqlite://")
    init_schema(engine)
    return SqliteIndexStore(make_session_factory(engine)())


def _events(store: SqliteIndexStore) -> list[SkillUsageEvent]:
    return list(store._s.scalars(select(SkillUsageEvent).order_by(SkillUsageEvent.at)))


class TestRecordContract:
    def test_record_writes_one_event(self) -> None:
        store = _store()
        svc = UsageService(store, _SwitchOn(), session_id="s_fixed")
        assert svc.record("demo", "get_skill", via=VIA_MCP, duration_ms=12, ok=True) is True

        rows = _events(store)
        assert len(rows) == 1
        assert rows[0].skill_id == "demo"
        assert rows[0].op == "get_skill"
        assert rows[0].via == "mcp"
        assert rows[0].session_id == "s_fixed"
        assert rows[0].duration_ms == 12
        assert rows[0].ok is True

    def test_switch_off_writes_nothing(self) -> None:
        """GWT：关掉采集后再跑一次工具调用 ⇒ 事件表零新增行。"""
        store = _store()
        svc = UsageService(store, _SwitchOff())
        assert svc.record("demo", "get_skill", via=VIA_MCP) is False
        assert _events(store) == []

    def test_no_skill_attribution_is_dropped(self) -> None:
        """N-01.1 排除项：Dashboard 浏览 / 全库 scan 这类没有 skill 归属的调用不记。"""
        store = _store()
        svc = UsageService(store, _SwitchOn())
        assert svc.record(None, "list_skills", via=VIA_CLI) is False
        assert svc.record("", "status_all", via=VIA_CLI) is False
        assert _events(store) == []

    def test_bad_via_is_rejected(self) -> None:
        """via 取值域固定 4 个（N-01.2），写错的入口标签不许混进聚合维度。"""
        store = _store()
        svc = UsageService(store, _SwitchOn())
        assert svc.record("demo", "get_skill", via="agent") is False
        assert _events(store) == []

    def test_param_digest_never_leaks_values(self) -> None:
        """N-11.6：只存摘要。同一参数两次摘要相同、不同参数不同，且不含原文。"""
        a = param_digest(path="a.md", dry_run=True)
        b = param_digest(dry_run=True, path="a.md")
        c = param_digest(path="b.md", dry_run=True)
        assert a == b and a != c
        assert "a.md" not in a

    def test_session_ids_are_unique(self) -> None:
        assert new_session_id() != new_session_id()


class TestDaemonHook:
    def _engine(self, tmp_path: Path) -> LocalEngine:
        home = tmp_path / "home"
        for rt in ("claude", "agents"):
            d = home / f".{rt}" / "skills" / "demo"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_bytes(
                b"---\nname: demo\nversion: 1.0.0\n"
                b"description: A demo skill used by usage telemetry tests only.\n---\nbody\n"
            )
        engine = LocalEngine(
            EnvPaths(repo_root=tmp_path / "repo", db_path=tmp_path / "state" / "s.db"),
            home=home,
        )
        engine.init_env()
        engine.import_skills()
        return engine

    def test_skill_op_is_recorded_as_web(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path)
        TaskExecutor(engine).execute("file_tree", {"slug": "demo"})

        rows = _events(engine.db)
        assert [(r.skill_id, r.op, r.via) for r in rows] == [("demo", "file_tree", "web")]

    def test_dashboard_ops_are_not_counted(self, tmp_path: Path) -> None:
        """scan/status/gate_summary/check/decay 都不算「使用」——否则面板自己刷出调用数。"""
        engine = self._engine(tmp_path)
        executor = TaskExecutor(engine)
        for op, params in (
            ("scan", {}),
            ("status", {}),
            ("gate_summary", {}),
            ("check", {"slug": "demo"}),
            ("decay", {"slug": "demo"}),
        ):
            executor.execute(op, params)
        assert _events(engine.db) == []

    def test_switch_off_blocks_daemon_recording(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path)
        engine.settings.set("usage_collect_enabled", False)
        TaskExecutor(engine).execute("file_tree", {"slug": "demo"})
        assert _events(engine.db) == []

    def test_telemetry_failure_does_not_break_task(self, tmp_path: Path, monkeypatch) -> None:
        """N-11.4 反向要求：采集炸了也不许影响任务返回。"""
        engine = self._engine(tmp_path)

        def boom(*_a, **_kw):
            raise RuntimeError("telemetry down")

        monkeypatch.setattr(engine.usage, "record", boom)
        result = TaskExecutor(engine).execute("file_tree", {"slug": "demo"})
        assert result.get("ok") is True
