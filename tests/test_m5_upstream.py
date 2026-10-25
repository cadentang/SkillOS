"""M5-02 上游跟踪启用（E-08.1 ~ E-08.5）。

口径逐条落成断言：

- **E-08.2**：`type=local` 恒 `current`；无 `upstream_sources` 记录 → 维度 `unknown`；
- **E-08.3**：`outdated` → decay `degraded`；`error/unknown` → `unknown`；
- **E-08.4**：连续 3 次网络失败才 `error`（单次抖动不改状态）；
- **E-08.5**：decay 的 unknown 断言随启用更新（旧的"恒 unknown"不再成立）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skillos.core.algorithm.decay import DecayStatus, evaluate_upstream
from skillos.core.fs.upstream import UpstreamSource as ProbeSource
from skillos.core.fs.upstream import UpstreamStatusKind, decide_status
from skillos.core.fs.version import GitError
from skillos.services.engine import EnvPaths, LocalEngine
from skillos.services.upstream_service import FAILURE_THRESHOLD, UpstreamService
from skillos.store.index_repo import SkillQuery, UpstreamSourceRecord

_SKILL_MD = (
    b"---\nname: demo\nversion: 1.0.0\n"
    b"description: a demo skill doing useful everyday work\n---\nbody\n"
)


@pytest.fixture()
def engine(tmp_path: Path):
    home = tmp_path / "home"
    d = home / ".claude" / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(_SKILL_MD)
    eng = LocalEngine(
        EnvPaths(repo_root=tmp_path / "repo", db_path=tmp_path / "state" / "skillos.db"),
        home=home,
    )
    eng.init_env()
    eng.import_skills()
    return eng


def _add_source(engine: LocalEngine, **kw) -> None:
    row = engine.db.get_skill_by_slug("demo")
    assert row is not None
    engine.db.upsert_upstream_source(UpstreamSourceRecord(skill_id=row.id, **kw))


class TestDecisionTable:
    def test_local_is_always_current(self) -> None:
        src = ProbeSource("s1", "local", None, None, None, None)
        assert decide_status(src, None).status == UpstreamStatusKind.CURRENT

    def test_same_hash_is_current(self) -> None:
        src = ProbeSource("s1", "git", "https://x/y.git", "main", None, "abc")
        assert decide_status(src, "abc").status == UpstreamStatusKind.CURRENT

    def test_advanced_ref_is_outdated(self) -> None:
        src = ProbeSource("s1", "git", "https://x/y.git", "main", None, "abc")
        assert decide_status(src, "def").status == UpstreamStatusKind.OUTDATED

    def test_no_baseline_is_unknown_not_outdated(self) -> None:
        """首次比对没有 baseline：记录 hash，但**不**谎称 outdated。"""
        src = ProbeSource("s1", "git", "https://x/y.git", "main", None, None)
        status = decide_status(src, "def")
        assert status.status == UpstreamStatusKind.UNKNOWN
        assert status.upstream_hash == "def"

    def test_marketplace_is_unknown(self) -> None:
        src = ProbeSource("s1", "marketplace", "mkt://x", None, None, "h")
        assert decide_status(src, "h").status == UpstreamStatusKind.UNKNOWN


class TestDecayMapping:
    @pytest.mark.parametrize("status,want", [
        ("current", DecayStatus.OK),
        ("outdated", DecayStatus.DEGRADED),
        ("error", DecayStatus.UNKNOWN),
        ("unknown", DecayStatus.UNKNOWN),
        (None, DecayStatus.UNKNOWN),
    ])
    def test_mapping(self, status, want) -> None:
        assert evaluate_upstream(status)[0] == want


class TestService:
    def test_no_source_record_is_unknown(self, engine) -> None:
        service = UpstreamService(engine.db, engine.git)
        assert service.check("demo").status == "unknown"
        # decay 同步是 unknown（E-08.2 / E-02.5 更新后的口径）
        assert engine.decay_metrics("demo")["upstream_divergence"] == "unknown"

    def test_local_source_is_current_and_decay_ok(self, engine) -> None:
        _add_source(engine, type="local", status="unknown")
        service = UpstreamService(engine.db, engine.git)
        assert service.check("demo").status == "current"
        assert engine.decay_metrics("demo")["upstream_divergence"] == "ok"

    def test_outdated_drives_degraded(self, engine) -> None:
        """E-08.3 GWT：上游 ref 前进 → outdated 且 decay degraded。"""
        _add_source(engine, type="git", url="https://example.invalid/x.git",
                    ref="main", upstream_hash="old")
        service = UpstreamService(
            engine.db, engine.git, probe=lambda url, ref: "new",
        )
        assert service.check("demo").status == "outdated"
        assert engine.decay_metrics("demo")["upstream_divergence"] == "degraded"

    def test_three_failures_then_error(self, engine) -> None:
        """E-08.4：连续失败满 3 次才 error，之前保持原状态（单次抖动不降级）。"""
        _add_source(engine, type="git", url="https://example.invalid/x.git",
                    ref="main", upstream_hash="old", status="current")

        def _boom(url, ref):
            raise GitError("network unreachable")

        service = UpstreamService(engine.db, engine.git, probe=_boom)
        outcomes = [service.check("demo").status for _ in range(FAILURE_THRESHOLD)]
        assert outcomes[:-1] == ["current"] * (FAILURE_THRESHOLD - 1)
        assert outcomes[-1] == "error"
        assert engine.decay_metrics("demo")["upstream_divergence"] == "unknown"

    def test_success_resets_failure_counter(self, engine) -> None:
        _add_source(engine, type="git", url="https://example.invalid/x.git",
                    ref="main", upstream_hash="old", status="current")
        state = {"fail": True}

        def _flaky(url, ref):
            if state["fail"]:
                raise GitError("boom")
            return "old"

        service = UpstreamService(engine.db, engine.git, probe=_flaky)
        service.check("demo")
        row = engine.db.get_skill_by_slug("demo")
        assert row is not None
        assert engine.db.get_upstream_source(row.id).consecutive_failures == 1

        state["fail"] = False
        assert service.check("demo").status == "current"
        assert engine.db.get_upstream_source(row.id).consecutive_failures == 0

    def test_check_all_only_visits_declared_sources(self, engine) -> None:
        """E-08.2：没有上游记录的 skill 连网络都不碰。"""
        calls: list[str] = []
        service = UpstreamService(
            engine.db, engine.git,
            probe=lambda url, ref: calls.append(url) or "h",
        )
        result = service.check_all()
        assert result["checked"] == 0
        assert calls == []

        _add_source(engine, type="git", url="https://example.invalid/x.git",
                    ref="main", upstream_hash="h")
        result = service.check_all()
        assert result["checked"] == 1
        assert result["summary"] == {"current": 1}
        assert calls == ["https://example.invalid/x.git"]

    def test_manual_op_registered(self) -> None:
        """E-08.1：手动「立即检查」= 具名 op（任务表即白名单）。"""
        from skillos.core.constants import WS_TASK_OPS

        assert "upstream_check" in WS_TASK_OPS


class TestSchema:
    def test_failure_counter_column_exists(self, engine) -> None:
        row = engine.db.get_skill_by_slug("demo")
        assert row is not None
        _add_source(engine, type="git", url="u", ref="main", status="unknown")
        src = engine.db.get_upstream_source(row.id)
        assert src is not None and src.consecutive_failures == 0
        assert len(engine.db.list_skills(SkillQuery(limit=10))) == 1
