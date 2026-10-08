"""分发器单测（04 §2.2.2 / FR-1.0.9 / FR-1.0.26 / FR-1.0.27 / FR-1.0.28）。

变异验证（03 §8.2）三件套，删守卫必红：
- test_recorded_rejects_modified —— FR-1.0.26 归属判定（上游 1.35.1：无证明即覆盖）
- test_self_verify_failure_restores —— FR-1.0.27 部署自证（上游：显示成功、盘上没有）
- test_multitarget_all_or_nothing —— FR-1.0.28 跨目标整体拒绝（上游 1.35.1 #322/#327）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skillos.core.algorithm.adapters.base import RuntimeAdapter
from skillos.core.algorithm.hash import aggregate_hash
from skillos.core.fs import distribute as dist
from skillos.core.fs.distribute import (
    BindingFacts,
    ReplacePolicy,
    ResidueReport,
    TargetRequest,
    check_residue,
    distribute_skill,
    recover_residue,
)
from skillos.core.model import FileEntry, SkillMeta, SkillSnapshot


class _Adapter(RuntimeAdapter):
    """测试用最小适配器：render_layout 用通用实现。"""

    key = "test"
    version = "1.0.0"
    supports = frozenset({"scripts", "references", "assets", "subskills"})
    field_policy = {"agent_created": "drop"}
    paths = {}

    def frontmatter_in(self, raw: dict) -> SkillMeta:  # pragma: no cover - pull 方向 P2 用
        return SkillMeta(name=raw.get("name", ""), description=raw.get("description", ""))


_ADAPTER = _Adapter()


def _snapshot(
    desc: str = "a skill for testing that does useful things",
    extra_files: tuple[FileEntry, ...] = (),
) -> SkillSnapshot:
    meta = SkillMeta(name="demo", description=desc)
    skill_md = f"---\nname: demo\ndescription: {desc}\n---\nbody\n".encode()
    files = (
        FileEntry("SKILL.md", skill_md),
        FileEntry("references/guide.md", b"guide\n"),
        *extra_files,
    )
    return SkillSnapshot(skill_id="s1", slug="demo", meta=meta, files=files)


def _facts(old_render: dict[str, bytes]) -> BindingFacts:
    """RECORDED 场景的标准基线事实。"""
    return BindingFacts(installed_hash=aggregate_hash(old_render), baseline_source="distributed")


def _old_render(extra: tuple[FileEntry, ...] = ()) -> dict[str, bytes]:
    """旧版渲染（RECORDED 场景的 installed 基线内容）。"""
    return _ADAPTER.render_layout(
        _snapshot(desc="old description of this skill here", extra_files=extra)
    )


def _write_disk(target: Path, files: dict[str, bytes]) -> None:
    for rel, content in files.items():
        p = target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)


def _disk_files(target: Path) -> dict[str, bytes]:
    return {
        p.relative_to(target).as_posix(): p.read_bytes()
        for p in sorted(target.rglob("*"))
        if p.is_file()
    }


@pytest.fixture()
def layout(tmp_path: Path):
    """deploy_root = <tmp>/rt/skills/demo；backups = <tmp>/repo/.skillos/backups。"""
    deploy = tmp_path / "rt" / "skills"
    deploy.mkdir(parents=True)
    backups = tmp_path / "repo" / ".skillos" / "backups"
    backups.mkdir(parents=True)
    return deploy, backups


def _target(deploy: Path, facts: BindingFacts | None = None) -> TargetRequest:
    return TargetRequest(adapter=_ADAPTER, deploy_root=deploy, facts=facts or BindingFacts())


class TestReplacePolicyRequired:
    def test_policy_has_no_default(self) -> None:
        """T13：ReplacePolicy 无默认值 —— 漏传必须是 TypeError 而非静默选一个。"""
        with pytest.raises(TypeError):
            distribute_skill(
                _snapshot(),
                "commit-1",
                [_target(Path("/nowhere"))],
                backups_dir=Path("/tmp/bk"),
            )


class TestOwnership:
    def test_fresh_install(self, layout) -> None:
        deploy, backups = layout
        snap = _snapshot()
        desired = _ADAPTER.render_layout(snap)
        report = distribute_skill(
            snap, "c1", [_target(deploy)], policy=ReplacePolicy.RECORDED, backups_dir=backups
        )
        assert report.ok
        out = report.outcomes[0]
        assert out.ok and out.wrote
        assert _disk_files(deploy / "demo") == desired
        assert out.new_installed_hash == aggregate_hash(desired)

    def test_idempotent_skip(self, layout) -> None:
        """NFR-5：目标内容 == 期望渲染 → 跳过写入。"""
        deploy, backups = layout
        snap = _snapshot()
        desired = _ADAPTER.render_layout(snap)
        _write_disk(deploy / "demo", desired)
        report = distribute_skill(
            snap, "c1", [_target(deploy)], policy=ReplacePolicy.RECORDED, backups_dir=backups
        )
        out = report.outcomes[0]
        assert out.ok and out.wrote is False
        assert out.new_installed_hash == aggregate_hash(desired)

    def test_recorded_allows_our_old_content(self, layout) -> None:
        """RECORDED：disk == installed_hash（我们装过的旧版）→ 允许更新（OUTDATED 路径）。"""
        deploy, backups = layout
        old_render = _old_render()
        _write_disk(deploy / "demo", old_render)
        facts = _facts(old_render)
        snap = _snapshot(desc="brand new description with more detail")
        report = distribute_skill(
            snap, "c2", [_target(deploy, facts)], policy=ReplacePolicy.RECORDED, backups_dir=backups
        )
        out = report.outcomes[0]
        assert out.ok and out.wrote
        desired = _ADAPTER.render_layout(snap)
        assert _disk_files(deploy / "demo") == desired

    def test_recorded_rejects_modified(self, layout) -> None:
        """变异验证 FR-1.0.26：disk != installed_hash（用户手改）→ TARGET_CONFLICT，
        逐字节保留。删掉归属判定本测试变红（上游 1.35.1 的核心事故）。"""
        deploy, backups = layout
        old_render = _old_render()
        _write_disk(deploy / "demo", old_render)
        facts = _facts(old_render)
        # 用户手改磁盘副本
        (deploy / "demo" / "SKILL.md").write_bytes(
            b"---\nname: demo\ndescription: user edited this\n---\nuser body\n"
        )
        before = _disk_files(deploy / "demo")
        report = distribute_skill(
            _snapshot(), "c2", [_target(deploy, facts)],
            policy=ReplacePolicy.RECORDED, backups_dir=backups,
        )
        out = report.outcomes[0]
        assert not out.ok
        assert out.error_code == "TARGET_CONFLICT"
        assert out.wrote is False
        assert _disk_files(deploy / "demo") == before  # 逐字节保留

    def test_recorded_rejects_unmanaged_existing(self, layout) -> None:
        """目标存在但无 binding 记录（installed_hash=None）→ 无法证明属于我们 → 拒绝。"""
        deploy, backups = layout
        _write_disk(deploy / "demo", {"SKILL.md": b"---\nname: demo\ndescription: x\n---\n"})
        report = distribute_skill(
            _snapshot(), "c1", [_target(deploy)],  # 无 facts
            policy=ReplacePolicy.RECORDED, backups_dir=backups,
        )
        assert report.outcomes[0].error_code == "TARGET_CONFLICT"

    def test_no_clobber_rejects_any_existing(self, layout) -> None:
        """NO_CLOBBER：目标非空即拒（导出 / import 换源场景），即使能证明是我们的。"""
        deploy, backups = layout
        old_render = _old_render()
        _write_disk(deploy / "demo", old_render)
        facts = _facts(old_render)
        report = distribute_skill(
            _snapshot(), "c2", [_target(deploy, facts)],
            policy=ReplacePolicy.NO_CLOBBER, backups_dir=backups,
        )
        out = report.outcomes[0]
        assert not out.ok and out.error_code == "TARGET_CONFLICT"
        assert _disk_files(deploy / "demo") == old_render

    def test_no_clobber_allows_absent_and_empty(self, layout) -> None:
        deploy, backups = layout
        report = distribute_skill(
            _snapshot(), "c1", [_target(deploy)],
            policy=ReplacePolicy.NO_CLOBBER, backups_dir=backups,
        )
        assert report.ok and report.outcomes[0].wrote

    def test_user_confirmed_overwrites_modified(self, layout) -> None:
        """USER_CONFIRMED：人类已显式确认 → 允许覆盖手改内容（Web 人工路径）。"""
        deploy, backups = layout
        old_render = _old_render()
        _write_disk(deploy / "demo", old_render)
        (deploy / "demo" / "SKILL.md").write_bytes(b"---\nname: demo\ndescription: user\n---\nx\n")
        facts = _facts(old_render)
        snap = _snapshot()
        report = distribute_skill(
            snap, "c2", [_target(deploy, facts)],
            policy=ReplacePolicy.USER_CONFIRMED, backups_dir=backups,
        )
        assert report.ok and report.outcomes[0].wrote
        assert _disk_files(deploy / "demo") == _ADAPTER.render_layout(snap)


class TestRemovalsGuard:
    """removals 场景：文件在旧版渲染里（进了 installed_hash）、被新版 canonical 删除。
    用户后加的文件（不在任何渲染里）会先被 RECORDED 归属判定拦下 —— 那也是保护。"""

    def _setup(self, layout, old_extra: tuple[FileEntry, ...]):
        deploy, backups = layout
        old_render = _old_render(old_extra)
        _write_disk(deploy / "demo", old_render)
        facts = _facts(old_render)
        return deploy, backups, facts

    def test_removals_held_back(self, layout) -> None:
        """变异验证 FR-1.0.26 / 07 D4：旧版有 templates/、新版删掉 → 停下列清单，
        不写。删守卫本测试变红（上游 1.34.0 丢 PPT 模板事故）。"""
        deploy, backups, facts = self._setup(
            layout, (FileEntry("templates/ppt.pptx", b"PK\x03\x04template"),)
        )
        report = distribute_skill(
            _snapshot(), "c2", [_target(deploy, facts)],
            policy=ReplacePolicy.RECORDED, backups_dir=backups,
        )
        out = report.outcomes[0]
        assert not out.ok
        assert out.error_code == "REMOVALS_REQUIRE_CONFIRM"
        assert out.held_back_removals == ["templates/"]
        # 未确认 → 一个字都不写，用户文件原样保留
        assert (deploy / "demo" / "templates" / "ppt.pptx").read_bytes() == b"PK\x03\x04template"

    def test_removals_confirmed_exact_set(self, layout) -> None:
        """确认集合与 removals 完全一致 → 执行（目录只报一行，确认也按目录）。"""
        deploy, backups, facts = self._setup(
            layout, (FileEntry("templates/ppt.pptx", b"PK\x03\x04template"),)
        )
        report = distribute_skill(
            _snapshot(), "c2", [_target(deploy, facts)],
            policy=ReplacePolicy.RECORDED, backups_dir=backups,
            confirmed_removals=frozenset({"templates/"}),
        )
        out = report.outcomes[0]
        assert out.ok and out.wrote
        assert not (deploy / "demo" / "templates").exists()

    def test_confirmation_bound_to_content(self, layout) -> None:
        """NFR-11.5：确认绑定精确路径集合 —— 集合不一致（旧确认 / 部分确认）不得放行。"""
        deploy, backups, facts = self._setup(
            layout,
            (FileEntry("templates/a.pptx", b"x"), FileEntry("notes.md", b"n")),
        )
        # 只确认了 templates/，没确认根级 notes.md → 整体仍拦
        report = distribute_skill(
            _snapshot(), "c2", [_target(deploy, facts)],
            policy=ReplacePolicy.RECORDED, backups_dir=backups,
            confirmed_removals=frozenset({"templates/"}),
        )
        out = report.outcomes[0]
        assert not out.ok
        assert set(out.held_back_removals) == {"templates/", "notes.md"}

    def test_untracked_asset_preserved_not_removal(self, layout) -> None:
        """FR-1.0.20：超阈值未纳管资产 → 回收保留、进 skipped_assets，绝不进 removals。"""
        deploy, backups = layout
        old_render = _old_render()
        _write_disk(deploy / "demo", old_render)
        big = b"\x00" * (6 * 1024 * 1024)  # > 5MB 阈值
        _write_disk(deploy / "demo", {"assets/huge.bin": big})
        facts = _facts(old_render)
        report = distribute_skill(
            _snapshot(), "c2", [_target(deploy, facts)],
            policy=ReplacePolicy.RECORDED, backups_dir=backups,
        )
        out = report.outcomes[0]
        assert out.ok and out.wrote
        assert out.held_back_removals == []
        assert out.skipped_assets == ["assets/huge.bin"]
        assert (deploy / "demo" / "assets" / "huge.bin").read_bytes() == big  # 未被删


class TestSelfVerify:
    def test_self_verify_failure_restores(self, layout, monkeypatch) -> None:
        """变异验证 FR-1.0.27：写后回读不一致 → 按失败处理并从备份还原。
        删回读步骤本测试变红（上游：显示成功、Agent 里什么都没有）。"""
        deploy, backups = layout
        old_render = _old_render()
        _write_disk(deploy / "demo", old_render)
        facts = _facts(old_render)

        real_read = Path.read_bytes

        def _corrupt_read(self: Path) -> bytes:
            data = real_read(self)
            return data + b"corrupted"

        # 只污染部署自证的回读阶段：目标目录下的读
        monkeypatch.setattr(dist, "_read_target_file", _corrupt_read)
        report = distribute_skill(
            _snapshot(), "c2", [_target(deploy, facts)],
            policy=ReplacePolicy.RECORDED, backups_dir=backups,
        )
        monkeypatch.undo()
        out = report.outcomes[0]
        assert not out.ok
        assert out.error_code == "VERIFY_FAILED"
        # 从备份还原：目标回到分发前状态
        assert _disk_files(deploy / "demo") == old_render


class TestCrossTarget:
    """FR-1.0.28：同一 Skill 多 Runtime —— 任一目标不安全 → 整体拒绝，零写入。"""

    def _two_targets(self, tmp_path: Path):
        deploy_a = tmp_path / "rtA" / "skills"
        deploy_b = tmp_path / "rtB" / "skills"
        deploy_a.mkdir(parents=True)
        deploy_b.mkdir(parents=True)
        backups = tmp_path / "repo" / ".skillos" / "backups"
        backups.mkdir(parents=True)
        old_render = _old_render()
        _write_disk(deploy_a / "demo", old_render)
        _write_disk(deploy_b / "demo", old_render)
        facts = _facts(old_render)
        return deploy_a, deploy_b, backups, facts, old_render

    def test_multitarget_all_or_nothing(self, tmp_path: Path) -> None:
        """B 被用户手改 → A（安全）也一个字都不写（上游 1.35.1 #322/#327）。"""
        deploy_a, deploy_b, backups, facts, old_render = self._two_targets(tmp_path)
        (deploy_b / "demo" / "SKILL.md").write_bytes(b"---\nname: demo\ndescription: mod\n---\n")
        report = distribute_skill(
            _snapshot(), "c2",
            [_target(deploy_a, facts), _target(deploy_b, facts)],
            policy=ReplacePolicy.RECORDED, backups_dir=backups,
        )
        assert not report.ok
        assert report.error_code == "PARTIAL_REJECT"
        # A 未被写入：仍是旧渲染
        assert _disk_files(deploy_a / "demo") == old_render
        statuses = [(o.runtime_key, o.error_code) for o in report.outcomes]
        assert statuses[0][1] == "PARTIAL_REJECT" or statuses[0][1] is None

    def test_multitarget_success(self, tmp_path: Path) -> None:
        deploy_a, deploy_b, backups, facts, _ = self._two_targets(tmp_path)
        snap = _snapshot()
        report = distribute_skill(
            snap, "c2",
            [_target(deploy_a, facts), _target(deploy_b, facts)],
            policy=ReplacePolicy.RECORDED, backups_dir=backups,
        )
        assert report.ok
        desired = _ADAPTER.render_layout(snap)
        assert _disk_files(deploy_a / "demo") == desired
        assert _disk_files(deploy_b / "demo") == desired

    def test_removals_on_any_target_rejects_all(self, tmp_path: Path) -> None:
        """任一目标命中 removals → 整体拒绝（含安全目标）。"""
        deploy_a, deploy_b, backups, facts, old_render = self._two_targets(tmp_path)
        # B 的旧版渲染里带 templates/（A 没有）→ B 命中 removals，A 安全但被牵连
        old_b = _ADAPTER.render_layout(
            _snapshot(
                desc="old description of this skill here",
                extra_files=(FileEntry("templates/x.pptx", b"t"),),
            )
        )
        _write_disk(deploy_b / "demo", old_b)
        facts_b = _facts(old_b)
        report = distribute_skill(
            _snapshot(), "c2",
            [_target(deploy_a, facts), _target(deploy_b, facts_b)],
            policy=ReplacePolicy.RECORDED, backups_dir=backups,
        )
        assert not report.ok
        assert _disk_files(deploy_a / "demo") == old_render  # A 未写


class TestDryRun:
    def test_dry_run_same_judgments_no_writes(self, layout) -> None:
        """FR-1.3.7：dry_run 与真跑共用同一判据（held_back / 冲突一致），且零写入。"""
        deploy, backups = layout
        old_render = _ADAPTER.render_layout(
            _snapshot(
                desc="old description of this skill here",
                extra_files=(FileEntry("templates/p.pptx", b"p"),),
            )
        )
        _write_disk(deploy / "demo", old_render)
        facts = _facts(old_render)
        dry = distribute_skill(
            _snapshot(), "c2", [_target(deploy, facts)],
            policy=ReplacePolicy.RECORDED, backups_dir=backups, dry_run=True,
        )
        real = distribute_skill(
            _snapshot(), "c2", [_target(deploy, facts)],
            policy=ReplacePolicy.RECORDED, backups_dir=backups,
        )
        # 判据一致
        assert dry.outcomes[0].error_code == real.outcomes[0].error_code
        assert dry.outcomes[0].held_back_removals == real.outcomes[0].held_back_removals
        # dry run 零写入
        assert dry.outcomes[0].wrote is False
        assert (deploy / "demo" / "templates" / "p.pptx").exists()
        assert not list(backups.rglob("*"))


class TestResidue:
    """FR-1.0.15 残留自检与恢复 —— 三形态判定 + 幂等恢复。"""

    def _target_dir(self, tmp_path: Path) -> Path:
        deploy = tmp_path / "rt" / "skills"
        deploy.mkdir(parents=True)
        return deploy / "demo"

    def test_form1_old_only_rolls_back(self, tmp_path: Path) -> None:
        target = self._target_dir(tmp_path)
        old = target.with_name(target.name + ".old")
        _write_disk(old, {"SKILL.md": b"---\nname: demo\ndescription: d\n---\nold\n"})
        report = check_residue(target)
        assert report.form == 1
        recover_residue(report)
        assert target.is_dir() and not old.exists()
        assert (target / "SKILL.md").read_bytes().endswith(b"old\n")
        # 幂等：恢复后再查是干净态
        assert check_residue(target).form == 0

    def test_form2_old_and_target_cleans_old(self, tmp_path: Path) -> None:
        target = self._target_dir(tmp_path)
        _write_disk(target, {"SKILL.md": b"---\nname: demo\ndescription: d\n---\nnew\n"})
        old = target.with_name(target.name + ".old")
        _write_disk(old, {"SKILL.md": b"old"})
        report = check_residue(target)
        assert report.form == 2
        recover_residue(report)
        assert target.is_dir() and not old.exists()
        assert (target / "SKILL.md").read_bytes().endswith(b"new\n")

    def test_form3_tmp_only_removed(self, tmp_path: Path) -> None:
        target = self._target_dir(tmp_path)
        tmp = target.with_name(f".{target.name}.tmp-x7f")
        _write_disk(tmp, {"SKILL.md": b"partial"})
        report = check_residue(target)
        assert report.form == 3
        recover_residue(report)
        assert not tmp.exists()
        assert check_residue(target).form == 0

    def test_clean(self, tmp_path: Path) -> None:
        target = self._target_dir(tmp_path)
        _write_disk(target, {"SKILL.md": b"---\nname: demo\ndescription: d\n---\n"})
        assert check_residue(target).form == 0


def test_backup_written_to_backups_dir(layout) -> None:
    """步骤 3：写入前先备份（进程存活时的失败恢复依据）。"""
    deploy, backups = layout
    old_render = _old_render()
    _write_disk(deploy / "demo", old_render)
    facts = _facts(old_render)
    distribute_skill(
        _snapshot(), "c2", [_target(deploy, facts)],
        policy=ReplacePolicy.RECORDED, backups_dir=backups,
    )
    assert any(backups.rglob("SKILL.md"))  # 备份里有旧内容


def test_residue_report_dataclass() -> None:
    r = ResidueReport(target=Path("/x"), form=0)
    assert r.form == 0


def test_policy_enum_values() -> None:
    assert {p.value for p in ReplacePolicy} == {"no_clobber", "recorded", "user_confirmed"}
