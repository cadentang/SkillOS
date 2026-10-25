"""V2-M2 收尾：管理者视图口径（N-01.7/8）+ 体检报告（N-02）+ 调用记录（N-11.7）。

三条口径必须单点、可断言：
- **僵尸 = 90 天零调用 ∩ staleness=degraded**，任一条件不满足就不是僵尸
  （N-01.7 拍板，旧口径不保留）；
- **报告是增量**：自上一份以来的新增，且无变化也要留痕（N-02.2 / N-02.6）；
- **调用链按 session 还原顺序**（N-11.7 L3：A → B → error）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from skillos.services.report_service import ReportService
from skillos.services.usage_service import UsageService
from skillos.store.db import init_schema, make_engine, make_session_factory
from skillos.store.index_repo import SqliteIndexStore
from skillos.store.models import HealthReport, SkillUsageEvent


class _Switch:
    def __init__(self, *, collect: bool = True, report: bool = False) -> None:
        self._collect, self._report = collect, report

    def collect_enabled(self) -> bool:
        return self._collect

    def report_enabled(self) -> bool:
        return self._report


def _store() -> SqliteIndexStore:
    engine = make_engine("sqlite://")
    init_schema(engine)
    return SqliteIndexStore(make_session_factory(engine)())


def _iso(days_ago: int = 0) -> date:
    return date.today() - timedelta(days=days_ago)


class TestManagerViewScope:
    """N-01.7 / N-01.8：Top、零调用、僵尸三个榜单。"""

    def _svc(self, store: SqliteIndexStore) -> UsageService:
        return UsageService(store, _Switch())

    def _daily(self, store: SqliteIndexStore, skill: str, count: int,
               days_ago: int = 0, via: str = "mcp") -> None:
        store.replace_usage_daily(_iso(days_ago), [{
            "device_id": "local", "skill_id": skill, "date": _iso(days_ago),
            "via": via, "count": count,
        }])

    def test_zombie_requires_both_conditions(self) -> None:
        """只 degraded 不算、只零调用也不算 —— 双条件缺一不可。"""
        store = _store()
        self._daily(store, "beta", 3)          # 近期有调用

        zombie = self._svc(store).zombie_slugs(
            slugs=["alpha", "beta", "gamma"],
            degraded={"alpha", "beta"},        # gamma 未 degraded
        )
        # alpha：零调用 + degraded → 僵尸；beta：degraded 但有调用；gamma：零调用但未 degraded
        assert zombie == ["alpha"]

    def test_top_and_zero_called(self) -> None:
        store = _store()
        # 聚合是「窗口内整体覆盖」：一次写入多行，分两次写会把前一批清掉（TQ-02 语义）
        store.replace_usage_daily(_iso(30), [
            {"device_id": "local", "skill_id": s, "date": _iso(0), "via": "mcp",
             "count": c}
            for s, c in (("alpha", 5), ("beta", 2))
        ])
        svc = self._svc(store)

        assert [r["skill_id"] for r in svc.top_called(days=30)] == ["alpha", "beta"]
        assert [r["count"] for r in svc.top_called(days=30)] == [5, 2]
        assert svc.zero_called(slugs=["alpha", "beta", "gamma"], days=30) == ["gamma"]

    def test_calls_outside_window_do_not_count(self) -> None:
        """窗口外的调用不算「近 30 天有调用」—— 否则僵尸名单永远为空。"""
        store = _store()
        store.replace_usage_daily(_iso(100), [{
            "device_id": "local", "skill_id": "alpha", "date": _iso(100),
            "via": "mcp", "count": 9,
        }])
        svc = self._svc(store)
        assert svc.top_called(days=30) == []
        assert svc.zero_called(slugs=["alpha"], days=30) == ["alpha"]

    def test_session_chain_keeps_order(self) -> None:
        """N-11.7 L3：同一 session 内按时间正序还原 A → B → error。"""
        store = _store()
        base = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
        for i, (slug, ok) in enumerate(
            [("alpha", True), ("beta", True), ("beta", False)]
        ):
            store._s.add(SkillUsageEvent(
                id=f"e{i}", session_id="s1", runtime="claude-code",
                skill_id=slug, op="get_skill", via="mcp",
                duration_ms=3, ok=ok, param_digest=None,
                at=base + timedelta(seconds=i),
            ))
        store._s.commit()

        svc = self._svc(store)
        events = svc.events_for("alpha")
        assert [e["op"] for e in events] == ["get_skill"]
        assert "param_digest" not in events[0]        # 参数摘要属隐私面，不进展示

        chains = svc.session_chains("alpha")
        assert len(chains) == 1
        assert [(s["slug"], s["ok"]) for s in chains[0]["steps"]] == [
            ("alpha", True), ("beta", True), ("beta", False),
        ]


class TestReportDiff:
    """N-02.2：报告 = 自上一份以来的增量；N-02.6：无变化也留痕。"""

    def _service(self, store: SqliteIndexStore, snap: dict) -> ReportService:
        return ReportService(store, lambda: snap)

    def test_first_report_treats_everything_as_new(self) -> None:
        store = _store()
        snap = {
            "states": [
                {"slug": "a", "runtime_key": "claude", "state": "SYNCED"},
                {"slug": "b", "runtime_key": "claude", "state": "CONFLICT"},
            ],
            "checks": [],
        }
        report = self._service(store, snap).generate()
        assert report["drift"]["counts"]["CONFLICT"]["now"] == 1
        # SYNCED 不进"新增漂移"（N-02.2 ① 只关心变坏的三态）
        assert report["drift"]["new_count"] == 1
        assert report["drift"]["new"] == [
            {"slug": "b", "runtime_key": "claude", "state": "CONFLICT"},
        ]
        assert report["unchanged"] is False

    def test_second_report_reports_net_change(self) -> None:
        """GWT：周一 2 个 CONFLICT、周三修 1 个 → 周报报净变化与新增 skill 名。"""
        store = _store()
        week1 = {
            "states": [
                {"slug": "a", "runtime_key": "claude", "state": "CONFLICT"},
                {"slug": "b", "runtime_key": "claude", "state": "CONFLICT"},
            ],
            "checks": [],
        }
        self._service(store, week1).generate()

        week2 = {
            "states": [
                {"slug": "a", "runtime_key": "claude", "state": "SYNCED"},
                {"slug": "b", "runtime_key": "claude", "state": "CONFLICT"},
            ],
            "checks": [{"slug": "b", "scope": "gate", "rule": "desc_min",
                        "level": "error", "message": "", "value": None}],
        }
        report = self._service(store, week2).generate()
        assert report["drift"]["counts"]["CONFLICT"] == {"now": 1, "prev": 2, "delta": -1}
        assert report["drift"]["new"] == []                       # 没有新增漂移
        assert report["gate"]["new"] == [{"slug": "b", "rule": "desc_min"}]
        assert report["unchanged"] is False

    def test_unchanged_still_persisted(self) -> None:
        store = _store()
        snap = {"states": [], "checks": []}
        svc = self._service(store, snap)
        first = svc.generate()
        second = svc.generate()

        assert first["unchanged"] is True and second["unchanged"] is True
        rows = list(store._s.scalars(select(HealthReport)))
        assert len(rows) == 2
        assert all(r.unchanged for r in rows)

    def test_newly_degraded_and_score_drops(self) -> None:
        store = _store()
        healthy = {"states": [], "checks": [
            {"slug": "a", "scope": "decay", "rule": "staleness", "level": "info",
             "message": "", "value": None},
        ]}
        svc_healthy = self._service(store, healthy)
        base = svc_healthy.generate()
        assert base["health"]["avg"] == 100.0

        degraded = {"states": [], "checks": [
            {"slug": "a", "scope": "decay", "rule": "staleness", "level": "warn",
             "message": "", "value": None},
            {"slug": "b", "scope": "decay", "rule": "staleness", "level": "warn",
             "message": "", "value": None},
        ]}
        report = self._service(store, degraded).generate()
        assert report["decay"]["newly_degraded"] == ["a", "b"]
        assert report["health"]["drops"][0]["slug"] == "a"
        assert report["health"]["drops"][0]["delta"] == -8        # HEALTH_WEIGHTS.degraded_decay

    def test_detail_excludes_baseline(self) -> None:
        """基线是给下一期算 diff 的，不该出现在详情响应里（payload 体积 + 语义）。"""
        store = _store()
        svc = self._service(store, {"states": [], "checks": []})
        created = svc.generate()

        detail = svc.get_report(created["id"])
        assert detail is not None and "baseline" not in detail

        summary = svc.list_reports(limit=5)[0]
        assert set(summary) >= {"id", "unchanged", "new_drift", "new_gate_errors"}

    def test_unknown_report_returns_none(self) -> None:
        store = _store()
        assert self._service(store, {"states": [], "checks": []}).get_report("nope") is None

    def test_drift_unavailable_is_surfaced(self) -> None:
        """空仓库（无 HEAD）≠ 全部同步：报告要写出"本期没法统计"的原因。"""
        store = _store()
        snap = {"states": [], "checks": [],
                "drift_unavailable": "仓库还没有提交（HEAD 不存在），本期不统计漂移"}
        report = self._service(store, snap).generate()
        assert report["drift"]["unavailable"]
        assert report["drift"]["counts"]["CONFLICT"] == {"now": 0, "prev": 0, "delta": 0}
