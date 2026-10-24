"""E-02.6 折旧接线：取证口径 + 落库（services/check_service.py）。

覆盖：
- 依赖声明字段口径（`dependencies` 优先、`requires` 回落、读不懂 → FORMAT_UNKNOWN）
- 引用预扫描（同引用方去重、依赖反向入度、未知 slug 不计）
- decay 三维 → skill_checks(scope=decay) 落库（E-01.3 的「未知 N 项」靠 level=unknown 数得出来）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from skillos.core.algorithm.decay import FORMAT_UNKNOWN, DecayStatus
from skillos.core.model import Commit, FileEntry, SkillMeta, SkillSnapshot
from skillos.services.check_service import (
    CheckService,
    ReferenceIndex,
    decay_rows,
    declared_deps,
)
from skillos.store.db import init_schema, make_engine, make_session_factory
from skillos.store.index_repo import SkillRecord, SqliteIndexStore
from skillos.store.models import SkillCheck


def _snap(slug: str, body: str = "", **extra) -> SkillSnapshot:
    meta = SkillMeta(name=slug, description="d", extra=extra)
    return SkillSnapshot(
        skill_id=slug, slug=slug, meta=meta,
        files=(FileEntry(path="SKILL.md", content=body.encode()),),
    )


class TestDeclaredDeps:
    def test_dependencies_field_wins(self) -> None:
        snap = _snap("a", dependencies=["b"], requires=["c"])
        assert declared_deps(snap) == ["b"]

    def test_requires_is_legacy_fallback(self) -> None:
        """历史数据写的是 requires；字段只在 dependencies 缺失时回落（不静默丢数据）。"""
        assert declared_deps(_snap("a", requires=["b"])) == ["b"]

    def test_absent_is_empty_list(self) -> None:
        assert declared_deps(_snap("a")) == []

    def test_non_list_is_format_unknown(self) -> None:
        """E-02.4：`dependencies: "b"`（字符串）⇒ 读不懂，不猜。"""
        assert declared_deps(_snap("a", dependencies="b")) is FORMAT_UNKNOWN

    def test_non_str_element_is_format_unknown(self) -> None:
        assert declared_deps(_snap("a", dependencies=["b", 3])) is FORMAT_UNKNOWN


class TestReferenceIndex:
    def test_counts_distinct_referrers(self) -> None:
        """同一个 skill 在两处提到同一目标只算 1 次，避免把「被引用」吹大。"""
        refs = ReferenceIndex.build(
            bodies={"a": "../b/ 和 skills/b/", "c": "skills/b/"},
            dep_edges=[],
            slugs=["a", "b", "c"],
        )
        assert refs.of("b") == 2

    def test_dependency_edge_adds_inbound(self) -> None:
        refs = ReferenceIndex.build(bodies={"a": "x"}, dep_edges=[("a", "b")], slugs=["a", "b"])
        assert refs.of("b") == 1

    def test_unknown_and_self_refs_ignored(self) -> None:
        refs = ReferenceIndex.build(
            bodies={"a": "../nope/ 和 ../a/"}, dep_edges=[], slugs=["a"],
        )
        assert refs.of("a") == 0
        assert refs.of("nope") == 0


class _FakeGit:
    """只实现 CheckService 用到的两个方法：批量快照 + 最近 commit。"""

    def __init__(self, snapshots: dict[str, SkillSnapshot], days_ago: dict[str, int | None]):
        self._snapshots = snapshots
        self._days = days_ago

    def snapshot_all(self, commit: str) -> dict[str, SkillSnapshot]:
        return self._snapshots

    def log(self, path: str | None = None, limit: int = 1) -> list[Commit]:
        slug = (path or "").removeprefix("skills/")
        days = self._days.get(slug)
        if days is None:
            return []
        at = datetime.now(UTC) - timedelta(days=days)
        return [Commit(sha="0" * 40, message="m", author="t", at=at.isoformat())]


def _store() -> SqliteIndexStore:
    engine = make_engine("sqlite://")
    init_schema(engine)
    return SqliteIndexStore(make_session_factory(engine)())


class TestDecayWiring:
    def _service(
        self, store: SqliteIndexStore | None = None
    ) -> tuple[CheckService, SqliteIndexStore]:
        store = store or _store()
        store.upsert_skill(SkillRecord(id="sk_alpha", slug="alpha", name="Alpha"))
        git = _FakeGit(
            snapshots={"alpha": _snap("alpha", body="../beta/", dependencies=["beta"])},
            days_ago={"alpha": 200},
        )
        svc = CheckService(git=git, store=store, head=lambda: "HEAD")
        return svc, store

    def test_decay_combines_three_dimensions(self) -> None:
        svc, _ = self._service()
        result = svc.decay("alpha")
        assert result.staleness is DecayStatus.DEGRADED      # 200 天且零被引用
        assert result.dependency_broken is DecayStatus.DEGRADED  # beta 不在库内
        assert result.upstream_divergence is DecayStatus.UNKNOWN
        assert result.value["days_since"] == 200
        assert result.value["missing_deps"] == ["beta"]

    def test_body_reference_relaxes_staleness(self) -> None:
        """正文被其他 skill 引用 ≥1 次 ⇒ 阈值放宽到 180（E-02.2）。"""
        store = _store()
        store.upsert_skill(SkillRecord(id="sk_alpha", slug="alpha", name="Alpha"))
        store.upsert_skill(SkillRecord(id="sk_gamma", slug="gamma", name="Gamma"))
        git = _FakeGit(
            snapshots={
                "alpha": _snap("alpha"),
                "gamma": _snap("gamma", body="see ../alpha/"),
            },
            days_ago={"alpha": 200},
        )
        result = CheckService(git=git, store=store, head=lambda: "HEAD").decay("alpha")
        assert result.value["inbound_refs"] == 1
        assert result.value["relaxed_by_refs"] is True
        assert result.value["threshold_days"] == 180
        assert result.staleness is DecayStatus.DEGRADED   # 200 > 180

    def test_record_decay_writes_one_row_per_dimension(self) -> None:
        svc, store = self._service()
        result = svc.decay("alpha")
        assert svc.record_decay("alpha", result) == 3

        rows = {
            r.rule: r
            for r in store._s.scalars(
                select(SkillCheck).where(SkillCheck.scope == "decay")
            )
        }
        assert set(rows) == {"staleness", "dependency_broken", "upstream_divergence"}
        # E-01.3：「未知 N 项」= scope=decay 且 level=unknown 的行数
        assert rows["upstream_divergence"].level == "unknown"
        assert rows["staleness"].level == "warn"        # degraded
        assert '"days_since": 200' in rows["staleness"].value

    def test_record_decay_is_idempotent(self) -> None:
        svc, store = self._service()
        result = svc.decay("alpha")
        svc.record_decay("alpha", result)
        svc.record_decay("alpha", result)
        count = len(list(store._s.scalars(
            select(SkillCheck).where(SkillCheck.scope == "decay")
        )))
        assert count == 3

    def test_unknown_skill_raises_instead_of_guessing(self) -> None:
        svc, _ = self._service()
        try:
            svc.decay("nope")
        except ValueError as e:
            assert "canonical" in str(e)
        else:
            raise AssertionError("不在 canonical 的 skill 必须报错，不得静默返回 unknown")


class TestDecayRows:
    def test_level_mapping_and_payload(self) -> None:
        rows = decay_rows({
            "staleness": "degraded",
            "dependency_broken_status": "ok",
            "upstream_divergence": "unknown",
            "value": {"days_since": 200, "relaxed_by_refs": True},
        })
        by_rule = {r["rule"]: r for r in rows}
        assert by_rule["staleness"]["level"] == "warn"
        assert by_rule["dependency_broken"]["level"] == "info"
        assert by_rule["upstream_divergence"]["level"] == "unknown"
        assert "200" in by_rule["staleness"]["value"]

    def test_missing_status_falls_back_to_unknown(self) -> None:
        rows = decay_rows({"value": {}})
        assert all(r["level"] == "unknown" for r in rows)
