"""V2-M2-01：遥测两张表的建表与迁移（docs/12 §3 Revision ①）。

两条路都要能走通：
1. 开发期 `init_schema`（create_all）—— SQLite 直接建表；
2. Alembic `upgrade head` —— 真实升级路径（正式口径，SQLite / PG 同构）。
另外验证 `(device_id, skill_id, date, via)` 唯一约束真的存在 —— 云端合并与补报去重
全靠它（V2-T9），约束丢了就是静默重复计数。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from skillos.store.db import init_schema, make_engine, make_session_factory
from skillos.store.models import SkillUsageDaily, SkillUsageEvent

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_DAILY_COLUMNS = {
    "id", "device_id", "skill_id", "date", "via", "count",
    "tokens_in", "tokens_out", "duration_ms_p50", "duration_ms_p95", "by_op", "created_at",
}
EXPECTED_EVENT_COLUMNS = {
    "id", "session_id", "runtime", "skill_id", "op", "via",
    "duration_ms", "ok", "param_digest", "tokens_in", "tokens_out", "at",
}


def _row(**over) -> SkillUsageDaily:
    base = dict(
        id="ulid1", device_id="local", skill_id="demo", date=date(2026, 10, 24),
        via="mcp", count=1,
    )
    base.update(over)
    return SkillUsageDaily(**base)


class TestCreateAll:
    def test_tables_have_documented_columns(self) -> None:
        engine = make_engine("sqlite://")
        init_schema(engine)
        inspector = inspect(engine)
        assert {c["name"] for c in inspector.get_columns("skill_usage_daily")} == \
            EXPECTED_DAILY_COLUMNS
        assert {c["name"] for c in inspector.get_columns("skill_usage_events")} == \
            EXPECTED_EVENT_COLUMNS

    def test_daily_unique_key_rejects_duplicate_bucket(self) -> None:
        """同一 (device, skill, date, via) 只能有一行 —— 否则聚合会重复计数。"""
        engine = make_engine("sqlite://")
        init_schema(engine)
        session = make_session_factory(engine)()
        session.add(_row())
        session.commit()
        session.add(_row(id="ulid2"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_event_defaults(self) -> None:
        engine = make_engine("sqlite://")
        init_schema(engine)
        session = make_session_factory(engine)()
        session.add(SkillUsageEvent(
            id="e1", session_id="s1", skill_id="demo", op="get_skill", via="mcp",
            at=datetime.now(UTC),
        ))
        session.commit()
        row = session.scalar(select(SkillUsageEvent))
        assert row is not None and row.ok is True and row.runtime == ""


class TestAlembicRevision:
    def test_upgrade_creates_both_tables(self, tmp_path, monkeypatch) -> None:
        db_path = tmp_path / "cloud.db"
        monkeypatch.setenv("SKILLOS_DB_URL", f"sqlite:///{db_path}")
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "data" / "migrations"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

        command.upgrade(cfg, "head")

        engine = make_engine(f"sqlite:///{db_path}")
        names = set(inspect(engine).get_table_names())
        assert {"skill_usage_daily", "skill_usage_events", "health_reports"} <= names
        columns = {c["name"] for c in inspect(engine).get_columns("skill_usage_daily")}
        assert columns == EXPECTED_DAILY_COLUMNS
        # M2-08：报告表的 payload / unchanged 是"展示 + 基线"双角色的落点
        report_cols = {c["name"] for c in inspect(engine).get_columns("health_reports")}
        assert report_cols == {
            "id", "device_id", "period_start", "period_end",
            "payload", "unchanged", "created_at",
        }

    def test_downgrade_removes_them(self, tmp_path, monkeypatch) -> None:
        db_path = tmp_path / "cloud2.db"
        monkeypatch.setenv("SKILLOS_DB_URL", f"sqlite:///{db_path}")
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "data" / "migrations"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        names = set(inspect(make_engine(f"sqlite:///{db_path}")).get_table_names())
        assert "skill_usage_daily" not in names
        assert "skill_usage_events" not in names
