"""纳管决策表单测（04 §2.2.5 / FR-1.0.16 / FR-1.0.21）。

坑：同一 slug 多副本是常态不是异常（本机 brainstorming 在 .claude 与 .agents
字节一致）—— 把多副本当错误处理，62 个 skill 首次纳管当场失败一半。
身份识别走 identity 分层匹配，不以 slug 字符串相等为唯一依据（07 N8）。
"""

from __future__ import annotations

from pathlib import Path

from skillos.core.algorithm.identity import IdentityCandidate
from skillos.core.fs.importer import (
    PRIMARY_ORDER,
    ImportPlan,
    plan_import,
)
from skillos.core.model import ScannedSkill, SkillMeta


def _scanned(
    rel_path: str,
    runtime_key: str,
    pack_hash: str = "h1",
    name: str | None = "demo",
) -> ScannedSkill:
    meta = SkillMeta(name=name, description="d") if name else None
    return ScannedSkill(
        root=Path("/rt"),
        rel_path=rel_path,
        runtime_key=runtime_key,
        scope="user",
        meta=meta,
        pack_hash=pack_hash,
        is_nested=False,
    )


class TestMergeDuplicates:
    def test_same_content_merged_one_skill_two_bindings(self) -> None:
        scanned = [
            _scanned("brainstorming", "claude", pack_hash="same"),
            _scanned("brainstorming", "agents", pack_hash="same"),
        ]
        plan = plan_import(scanned, existing=[])
        assert len(plan.skills) == 1
        skill = plan.skills[0]
        assert skill.slug == "brainstorming"
        assert len(skill.bindings) == 2
        assert {b.runtime_key for b in skill.bindings} == {"claude", "agents"}
        assert plan.conflicts == []
        assert plan.collisions == []

    def test_primary_order_claude_first(self) -> None:
        scanned = [
            _scanned("x", "codex", pack_hash="same"),
            _scanned("x", "claude", pack_hash="same"),
            _scanned("x", "agents", pack_hash="same"),
        ]
        plan = plan_import(scanned, existing=[])
        assert plan.skills[0].primary.runtime_key == "claude"
        assert PRIMARY_ORDER[0] == "claude"

    def test_single_copy(self) -> None:
        plan = plan_import([_scanned("solo", "claude")], existing=[])
        assert len(plan.skills) == 1
        assert plan.skills[0].primary.runtime_key == "claude"


class TestConflicts:
    def test_same_slug_different_content(self) -> None:
        """NAME_CONFLICT：目录名完全相同、内容不一致 → 用户选主副本。"""
        scanned = [
            _scanned("review", "claude", pack_hash="a"),
            _scanned("review", "agents", pack_hash="b"),
        ]
        plan = plan_import(scanned, existing=[])
        assert plan.skills == []
        assert len(plan.conflicts) == 1
        c = plan.conflicts[0]
        assert c.code == "NAME_CONFLICT"
        assert len(c.candidates) == 2

    def test_case_only_collision_rejected(self) -> None:
        """NAME_COLLISION（NFR-7.4）：Foo 与 foo 归一化同键且内容不同 → 拒绝。"""
        scanned = [
            _scanned("Foo", "claude", pack_hash="a"),
            _scanned("foo", "agents", pack_hash="b"),
        ]
        plan = plan_import(scanned, existing=[])
        assert plan.skills == []
        assert len(plan.collisions) == 1
        assert plan.collisions[0].code == "NAME_COLLISION"

    def test_case_only_same_content_merged(self) -> None:
        """归一化同键、内容一致 → 合并（大小写差异本身不是拒绝理由）。"""
        scanned = [
            _scanned("Foo", "claude", pack_hash="same"),
            _scanned("foo", "agents", pack_hash="same"),
        ]
        plan = plan_import(scanned, existing=[])
        assert len(plan.skills) == 1


class TestExisting:
    def test_already_imported_idempotent_binding(self) -> None:
        """同 slug 已纳管 → 幂等绑定既有 skill_id，不新建记录。"""
        scanned = [_scanned("known", "claude", pack_hash="h1")]
        existing = [IdentityCandidate(skill_id="s-42", content_hash="h1", dir_name="known")]
        plan = plan_import(scanned, existing=existing)
        assert plan.skills[0].skill_id == "s-42"

    def test_new_skill_gets_new_id_placeholder(self) -> None:
        plan = plan_import([_scanned("fresh", "claude")], existing=[])
        assert plan.skills[0].skill_id is None  # services 层落库时生成


class TestWarnings:
    def test_unparseable_meta_warning(self) -> None:
        plan = plan_import([_scanned("bad", "claude", name=None)], existing=[])
        assert any("bad" in w for w in plan.warnings)


def test_plan_type() -> None:
    p = ImportPlan(skills=[], conflicts=[], collisions=[], warnings=[])
    assert p.skills == []
