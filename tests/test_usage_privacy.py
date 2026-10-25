"""V2-M2-07：云端上报的隐私边界与去重合并（N-01.4/5/9，NFR-V2-03）。

这块的验收核心是「**能不能证明没上报**」：
- `usage_report_enabled=false`（默认）⇒ daemon 直接拒绝，报表行空，报文里无任何事件；
- 报文只含 N-01.5 允许的字段（skill 标识 + 计数 + 日期 + via + N-11.6 聚合列），
  不得出现 `param_digest`、Runtime 路径、事件 id；
- 服务端按 (device_id, skill, date, via) **覆盖**合并 —— 重发同一份数据不翻倍（N-01.9）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select

from skillos.daemon.main import TaskExecutor
from skillos.services.engine import EnvPaths, LocalEngine
from skillos.services.usage_service import UsageService
from skillos.store.cloud_repo import CloudRepo
from skillos.store.db import init_schema, make_engine, make_session_factory
from skillos.store.index_repo import SqliteIndexStore
from skillos.store.models import SkillUsageDaily

ALLOWED_REPORT_KEYS = {
    "device_id", "skill_id", "date", "via", "count",
    "tokens_in", "tokens_out", "duration_ms_p50", "duration_ms_p95", "by_op",
}


def _store() -> SqliteIndexStore:
    engine = make_engine("sqlite://")
    init_schema(engine)
    return SqliteIndexStore(make_session_factory(engine)())


def _engine(tmp_path: Path) -> LocalEngine:
    home = tmp_path / "home"
    d = home / ".claude" / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(
        b"---\nname: demo\nversion: 1.0.0\n"
        b"description: A demo skill used by usage report privacy tests only.\n---\nbody\n"
    )
    engine = LocalEngine(
        EnvPaths(repo_root=tmp_path / "repo", db_path=tmp_path / "state" / "s.db"),
        home=home,
    )
    engine.init_env()
    engine.import_skills()
    return engine


class TestZeroReportingByDefault:
    def test_default_off_returns_empty_rows(self) -> None:
        store = _store()
        store.add_usage_event(
            session_id="s", runtime="", skill_id="demo", op="get_skill", via="mcp",
            duration_ms=3, ok=True, param_digest="abc",
        )
        UsageService(store, _settings(store)).aggregate()

        svc = UsageService(store, _settings(store))
        assert svc.report_rows() == []          # 默认 off ⇒ 零上报，可断言

    def test_daemon_op_refuses_when_disabled(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.usage.record("demo", "get_skill", via="mcp")
        engine.usage.aggregate()

        result = TaskExecutor(engine).execute("usage_report", {})
        assert result["ok"] is False
        assert result["error"]["code"] == "REPORT_DISABLED"


class TestReportPayload:
    def test_payload_contains_only_aggregate_columns(self, tmp_path: Path) -> None:
        """NFR-V2-03：报文只有聚合值 —— 事件、参数摘要、路径一律不出现。"""
        engine = _engine(tmp_path)
        engine.settings.set("usage_report_enabled", True)
        engine.usage.record("demo", "get_skill", via="mcp", digest="deadbeef")
        engine.usage.aggregate()

        rows = engine.usage.report_rows()
        assert rows, "开关开启后必须能报出聚合行"
        for row in rows:
            assert set(row) <= ALLOWED_REPORT_KEYS
            assert "deadbeef" not in str(row)
            assert str(tmp_path) not in str(row)      # 本机路径不上云（N-11.9⑤）

    def test_daemon_op_returns_rows_when_enabled(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.settings.set("usage_report_enabled", True)
        engine.usage.record("demo", "get_skill", via="mcp")
        engine.usage.aggregate()

        result = TaskExecutor(engine).execute("usage_report", {})
        assert result["ok"] is True
        assert result["data"]["rows"][0]["skill_id"] == "demo"


def _settings(store: SqliteIndexStore):
    from skillos.services.settings_service import SettingsService

    return SettingsService(store)


class TestCloudMerge:
    def _cloud(self) -> CloudRepo:
        engine = make_engine("sqlite://")
        init_schema(engine)
        return CloudRepo(make_session_factory(engine)())

    def _row(self, **over) -> dict:
        base = {
            "device_id": "dev-1", "skill_id": "demo", "date": "2026-10-24",
            "via": "mcp", "count": 3, "tokens_in": 100, "tokens_out": 50,
            "duration_ms_p50": 12, "duration_ms_p95": 30, "by_op": '{"get_skill": 3}',
        }
        base.update(over)
        return base

    def test_merge_is_idempotent(self) -> None:
        """N-01.9：同一份数据重发（断连补报）后云端不能翻倍。"""
        repo = self._cloud()
        repo.merge_usage_daily([self._row()])
        repo.merge_usage_daily([self._row()])

        rows = list(repo.s.scalars(select(SkillUsageDaily)))
        assert len(rows) == 1
        assert rows[0].count == 3

    def test_merge_updates_changed_counts(self) -> None:
        repo = self._cloud()
        repo.merge_usage_daily([self._row(count=3)])
        repo.merge_usage_daily([self._row(count=7)])
        rows = list(repo.s.scalars(select(SkillUsageDaily)))
        assert [r.count for r in rows] == [7]

    def test_devices_are_separate_buckets(self) -> None:
        repo = self._cloud()
        repo.merge_usage_daily([self._row(device_id="dev-1"), self._row(device_id="dev-2")])
        assert len(list(repo.s.scalars(select(SkillUsageDaily)))) == 2

    def test_accepts_iso_date_strings(self) -> None:
        """设备上报的 date 是 JSON 字符串，落库要转 date（团队聚合按日分组）。"""
        repo = self._cloud()
        repo.merge_usage_daily([self._row(date="2026-10-01")])
        row = repo.s.scalar(select(SkillUsageDaily))
        assert row is not None and row.date == date(2026, 10, 1)
        assert isinstance(row.date, date) and not isinstance(row.date, datetime)
        assert row.created_at is not None
        assert datetime.now(UTC).year == 2026   # 时钟无关的弱断言，避免时区假失败


class TestCloudSummary:
    """N-01.6：跨设备汇总只看总量，但**必须按租户设备集合取数**。

    `skill_usage_daily` 没有 user_id 列 —— 不按设备过滤就等于把别人的调用次数
    加进来，这是隐私面最容易漏的一处。
    """

    def _cloud(self) -> CloudRepo:
        engine = make_engine("sqlite://")
        init_schema(engine)
        return CloudRepo(make_session_factory(engine)())

    def _row(self, **over) -> dict:
        base = {
            "device_id": "dev-1", "skill_id": "demo",
            "date": datetime.now(UTC).date().isoformat(),
            "via": "mcp", "count": 3, "tokens_in": 100, "tokens_out": 50,
        }
        base.update(over)
        return base

    def test_sums_across_devices(self) -> None:
        repo = self._cloud()
        repo.merge_usage_daily([
            self._row(device_id="dev-1", count=3),
            self._row(device_id="dev-2", count=4),
        ])
        items = repo.usage_summary(device_ids=["dev-1", "dev-2"])
        assert [(i["skill_id"], i["count"], i["device_count"]) for i in items] == [
            ("demo", 7, 2),
        ]

    def test_other_users_devices_excluded(self) -> None:
        repo = self._cloud()
        repo.merge_usage_daily([
            self._row(device_id="mine", count=3),
            self._row(device_id="someone-else", count=99),
        ])
        items = repo.usage_summary(device_ids=["mine"])
        assert items[0]["count"] == 3
        assert items[0]["device_count"] == 1

    def test_empty_device_set_returns_nothing(self) -> None:
        """无设备不得退化成"全表"——那是最隐蔽的跨租户泄漏。"""
        repo = self._cloud()
        repo.merge_usage_daily([self._row(device_id="dev-1")])
        assert repo.usage_summary(device_ids=[]) == []
