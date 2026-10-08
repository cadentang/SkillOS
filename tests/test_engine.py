"""引擎闭环集成测试（p1-engine-close 验收）：init → import → status → push。

在临时环境里复刻「本机四端」结构，走完 demo 主线的数据流：
纳管（imported 基线）→ 首次 push（转 distributed）→ 编辑 canonical →
OUTDATED → 再 push → SYNCED；门禁 error 拦截 push（demo 高潮一的引擎侧）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skillos.core.model import DriftState
from skillos.services.engine import EnvPaths, LocalEngine

_SKILL_MD = (
    b"---\nname: demo\nversion: 1.2.0\n"
    b"description: a demo skill doing useful work\n---\nbody\n"
)


@pytest.fixture()
def env(tmp_path: Path):
    home = tmp_path / "home"
    # 复刻四端目录结构（claude + agents 双副本，workbuddy/codex 空置）
    for rt in ("claude", "agents"):
        d = home / f".{rt}" / "skills" / "demo"
        (d / "references").mkdir(parents=True)
        (d / "SKILL.md").write_bytes(_SKILL_MD)
        (d / "references" / "guide.md").write_bytes(b"guide\n")
    paths = EnvPaths(repo_root=tmp_path / "repo", db_path=tmp_path / "state" / "skillos.db")
    engine = LocalEngine(paths, home=home)
    return engine, home


class TestClosedLoop:
    def test_init_idempotent(self, env) -> None:
        engine, _ = env
        assert engine.init_env()
        assert engine.init_env()  # 幂等

    def test_scan_finds_both_copies(self, env) -> None:
        engine, _ = env
        scanned = engine.scan_all()
        assert len(scanned) == 2  # claude + agents 各一份（canonical path 不合并：根不同）

    def test_import_merges_duplicates(self, env) -> None:
        """同 slug 双副本字节一致 → 合并一条 Skill + 两个 binding（04 §2.2.5）。"""
        engine, _ = env
        engine.init_env()
        plan = engine.import_skills()
        assert len(plan.skills) == 1
        assert plan.conflicts == [] and plan.collisions == []
        assert len(plan.skills[0].bindings) == 2
        row = engine.db.get_skill_by_slug("demo")
        assert row is not None and row.name == "demo"
        assert len(engine.db.list_bindings(row.id)) == 2
    def test_status_imported_then_push_synced(self, env) -> None:
        """imported 基线（两方比对）→ 首次 push → distributed 三方比对 SYNCED。"""
        engine, _ = env
        engine.init_env()
        engine.import_skills()

        # imported：磁盘内容 vs 渲染（frontmatter 规范化后）不等 → MODIFIED（待确认）
        cells = engine.status_matrix()
        assert len(cells) == 2
        assert all(c.state == DriftState.MODIFIED for c in cells)

        # 首次 push：RECORDED 用 imported 锚点（disk == 纳管时内容）→ 放行
        report = engine.push_skill("demo")
        assert report.ok, [(o.error_code, o.detail) for o in report.outcomes]
        assert all(o.wrote for o in report.outcomes)

        # push 后：distributed 三方比对 → SYNCED（workbuddy/codex 首装后也有了 binding）
        cells = engine.status_matrix()
        assert len(cells) == 4
        assert all(c.state == DriftState.SYNCED for c in cells)
        sid = engine.db.get_skill_by_slug("demo").id
        assert engine.aggregate_status() == {sid: DriftState.SYNCED}

    def test_edit_canonical_outdated_then_push(self, env) -> None:
        """demo 主线：编辑 canonical → OUTDATED → push → 四端 SYNCED。"""
        engine, _ = env
        engine.init_env()
        engine.import_skills()
        assert engine.push_skill("demo").ok

        skill_md = engine.paths.repo_root / "skills" / "demo" / "SKILL.md"
        skill_md.write_bytes(
            b"---\nname: demo\nversion: 1.3.0\n"
            b"description: updated desc with more detail\n---\nnew body\n"
        )
        engine.git.commit(["skills/demo"], "skillos: edit demo")

        cells = engine.status_matrix()
        assert all(c.state == DriftState.OUTDATED for c in cells)

        report = engine.push_skill("demo")
        assert report.ok
        # yaml.safe_dump(sort_keys=True)：frontmatter 字段按字母序，不能断言首字段是 name
        for c in cells:
            path = engine.home / f".{c.runtime_key}" / "skills" / "demo" / "SKILL.md"
            rendered = path.read_bytes()
            assert b"name: demo" in rendered
            assert b"new body" in rendered
        assert all(c.state == DriftState.SYNCED for c in engine.status_matrix())

    def test_gate_blocks_push(self, env) -> None:
        """demo 高潮一（引擎侧）：删 description → 门禁 error → push 被拦（FR-1.0.11）。"""
        engine, _ = env
        engine.init_env()
        engine.import_skills()
        assert engine.push_skill("demo").ok

        skill_md = engine.paths.repo_root / "skills" / "demo" / "SKILL.md"
        skill_md.write_bytes(b"---\nname: demo\n---\nbody without description\n")
        engine.git.commit(["skills/demo"], "skillos: break demo")

        report = engine.push_skill("demo")
        assert not report.ok
        assert report.outcomes[0].error_code == "GATE_FAILED"
        assert "fm.description.required" in (report.outcomes[0].detail or "")

        # --allow-gate-failure（CLI 专属开关）可继续
        forced = engine.push_skill("demo", allow_gate_failure=True)
        assert forced.ok

    def test_push_unknown_slug_raises(self, env) -> None:
        engine, _ = env
        engine.init_env()
        with pytest.raises(ValueError, match="未纳管"):
            engine.push_skill("nope")

    def test_unmanaged_runtime_not_in_matrix(self, env) -> None:
        """「不适用」（磁盘无目录且无 binding）的格子不进矩阵（04 §2.1.3）。"""
        engine, _ = env
        engine.init_env()
        engine.import_skills()
        # 未 push：只有 claude/agents 有磁盘副本，workbuddy/codex「不适用」不进矩阵
        keys = {c.runtime_key for c in engine.status_matrix()}
        assert keys == {"claude", "agents"}
