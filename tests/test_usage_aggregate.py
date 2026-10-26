"""V2-M2-04：日聚合重算与清理（N-11.3 / TQ-02 / docs/12 §5.2）。

关键性质：
- **幂等**：同一批事件重算两次，日聚合行完全相同（定时任务重试不能把数字算翻倍）；
- **以事件表为源**：删掉事件后重算，旧聚合行随之消失；
- **清理在后**：30 天前事件清理不影响窗口内聚合结果。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from skillos.core.algorithm.usage import aggregate_buckets, percentile
from skillos.daemon.main import TaskExecutor
from skillos.services.engine import EnvPaths, LocalEngine
from skillos.services.usage_service import UsageService
from skillos.store.db import init_schema, make_engine, make_session_factory
from skillos.store.index_repo import SqliteIndexStore
from skillos.store.models import SkillUsageDaily, SkillUsageEvent


class _SwitchOn:
    def collect_enabled(self) -> bool:
        return True


def _store() -> SqliteIndexStore:
    engine = make_engine("sqlite://")
    init_schema(engine)
    return SqliteIndexStore(make_session_factory(engine)())


def _seed(store: SqliteIndexStore, *, when: datetime, via: str = "mcp",
          skill: str = "demo", op: str = "get_skill", duration: int | None = 10) -> None:
    store.add_usage_event(
        session_id="s1", runtime="", skill_id=skill, op=op, via=via,
        duration_ms=duration, ok=True, param_digest=None,
    )
    # 直接改时间，避免测试依赖「现在」
    row = store._s.scalars(select(SkillUsageEvent).order_by(SkillUsageEvent.at.desc())).first()
    assert row is not None
    row.at = when
    store._s.commit()


def _daily(store: SqliteIndexStore) -> list[dict]:
    rows = store._s.scalars(select(SkillUsageDaily).order_by(SkillUsageDaily.date))
    return [
        {"date": r.date, "skill": r.skill_id, "via": r.via, "count": r.count,
         "p50": r.duration_ms_p50, "by_op": r.by_op}
        for r in rows
    ]


class TestPureAggregate:
    def test_percentile_nearest_rank(self) -> None:
        assert percentile([], 0.5) is None
        assert percentile([5], 0.95) == 5
        assert percentile([1, 2, 3, 4], 0.5) == 2
        assert percentile([1, 2, 3, 4], 0.95) == 4

    def test_buckets_group_by_day_skill_via(self) -> None:
        base = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)

        class E:
            def __init__(self, at, skill, via, op, dur):
                self.at = at
                self.skill_id = skill
                self.via = via
                self.op = op
                self.duration_ms = dur

        rows = aggregate_buckets([
            E(base, "a", "mcp", "get_skill", 10),
            E(base, "a", "mcp", "get_skill", 30),
            E(base, "a", "cli", "push", None),
            E(base - timedelta(days=1), "a", "mcp", "get_skill", 5),
        ])
        assert [(r["date"], r["via"], r["count"]) for r in rows] == [
            (base.date() - timedelta(days=1), "mcp", 1),
            (base.date(), "cli", 1),
            (base.date(), "mcp", 2),
        ]
        assert rows[-1]["duration_ms_p50"] == 10
        assert rows[-1]["tokens_in"] == 0   # L2 未接：不估算


class TestAggregateService:
    def test_recompute_is_idempotent(self) -> None:
        store = _store()
        now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
        _seed(store, when=now - timedelta(hours=1))
        _seed(store, when=now - timedelta(hours=2), op="sync_push", via="cli")

        svc = UsageService(store, _SwitchOn())
        svc.aggregate(now=now)
        first = _daily(store)
        svc.aggregate(now=now)          # 再跑一次
        assert _daily(store) == first
        assert len(first) == 2
        assert {r["count"] for r in first} == {1}

    def test_events_are_the_source_of_truth(self) -> None:
        """TQ-02：聚合行可重算 —— 事件没了，重算后行也没了。"""
        store = _store()
        now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
        _seed(store, when=now - timedelta(hours=1))
        svc = UsageService(store, _SwitchOn())
        svc.aggregate(now=now)
        assert len(_daily(store)) == 1

        for row in list(store._s.scalars(select(SkillUsageEvent))):
            store._s.delete(row)
        store._s.commit()
        svc.aggregate(now=now)
        assert _daily(store) == []

    def test_replace_usage_daily_covers_conflicting_row(self) -> None:
        """窗口没盖住的旧行不能被 INSERT 撞死 —— 必须按冲突键覆盖。

        现实触发条件的复现：daemon 定时任务与 CLI 各自聚合同一个库，删除与写入之间
        隔着一次 flush，另一个进程先插进去的同键行会让裸 INSERT 抛 IntegrityError，
        一次撞车之后整个 Session 进入 rollback-only，后续全部读操作失败。
        """
        store = _store()
        day = datetime(2026, 10, 24, 12, 0, tzinfo=UTC).date()
        base = {
            "device_id": "local", "skill_id": "demo", "date": day, "via": "mcp",
            "count": 1, "tokens_in": 0, "tokens_out": 0, "by_op": '{"get_skill": 1}',
        }
        assert store.replace_usage_daily(day, [dict(base)]) == 1

        # since 落在 row 的日期之后 → 删除清不掉旧行，写入必然撞上唯一约束
        store.replace_usage_daily(
            day + timedelta(days=1), [dict(base, count=2, tokens_in=7)]
        )

        rows = list(store._s.scalars(select(SkillUsageDaily)))
        assert len(rows) == 1                    # 覆盖，不是并行一行也不是崩掉
        assert rows[0].count == 2                # 取本次重算值（重算覆盖，不累加）
        assert rows[0].tokens_in == 7
        assert rows[0].created_at is not None    # Core insert 必须显式带 created_at

    def test_purge_drops_only_expired_events(self) -> None:
        store = _store()
        now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
        _seed(store, when=now - timedelta(days=1))
        _seed(store, when=now - timedelta(days=40))

        svc = UsageService(store, _SwitchOn())
        svc.aggregate(now=now)
        assert svc.purge(now=now) == 1
        remaining = len(list(store._s.scalars(select(SkillUsageEvent))))
        assert remaining == 1
        assert len(_daily(store)) == 1     # 清理不影响窗口内聚合


class TestDaemonOp:
    def test_op_is_whitelisted_and_wired(self, tmp_path) -> None:
        from skillos.core.constants import WS_TASK_OPS

        assert "usage_aggregate" in WS_TASK_OPS

        home = tmp_path / "home"
        d = home / ".claude" / "skills" / "demo"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_bytes(
            b"---\nname: demo\nversion: 1.0.0\n"
            b"description: A demo skill used by usage aggregate tests only.\n---\nbody\n"
        )
        engine = LocalEngine(
            EnvPaths(repo_root=tmp_path / "repo", db_path=tmp_path / "state" / "s.db"),
            home=home,
        )
        engine.init_env()
        engine.import_skills()

        engine.usage.record("demo", "get_skill", via="mcp", duration_ms=7)
        result = TaskExecutor(engine).execute("usage_aggregate", {})
        assert result["ok"] is True
        assert result["data"]["rows"] == 1
        assert result["data"]["purged"] == 0
        assert _daily(engine.db)[0]["count"] == 1

    def test_aggregate_op_is_not_counted_as_usage(self, tmp_path) -> None:
        """维护任务不是「使用」——不能自己给自己刷调用数（N-01.1）。"""
        home = tmp_path / "home"
        (home / ".claude" / "skills").mkdir(parents=True)
        engine = LocalEngine(
            EnvPaths(repo_root=tmp_path / "repo", db_path=tmp_path / "state" / "s.db"),
            home=home,
        )
        engine.init_env()
        TaskExecutor(engine).execute("usage_aggregate", {})
        assert list(engine.db._s.scalars(select(SkillUsageEvent))) == []


@pytest.mark.parametrize("via", ["mcp", "web", "cli"])
def test_all_vias_aggregate_into_separate_buckets(via: str) -> None:
    store = _store()
    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    _seed(store, when=now, via=via)
    UsageService(store, _SwitchOn()).aggregate(now=now)
    assert [r["via"] for r in _daily(store)] == [via]
