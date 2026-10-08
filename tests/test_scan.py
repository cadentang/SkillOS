"""扫描器单测（04 §2.2.1 六条规则 / FR-1.0.1 / FR-1.0.3 / 06 A2）。

坑（06 A2，变异验证）：README.md 被误判成 Skill 标记时，父目录误判会
短路跳过其下的真 Skill —— 表现为"扫不到"，不是"扫错了"。父目录不是
包根时必须继续下钻。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from skillos.core.fs.scan import scan_root, scan_roots
from skillos.core.model import RootSpec


def _make_skill(parent: Path, name: str, body: bytes = b"---\nname: t\n---\nhello\n") -> Path:
    d = parent / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_bytes(body)
    return d


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    r = tmp_path / "rt"
    r.mkdir()
    return r


def _spec(root: Path) -> RootSpec:
    return RootSpec(runtime_key="claude", root=root, scope="user")


class TestSkillDetection:
    def test_skill_md_detected(self, root: Path) -> None:
        _make_skill(root, "alpha")
        skills = scan_root(root, _spec(root), frozenset())
        assert [s.rel_path for s in skills] == ["alpha"]

    def test_lowercase_legacy_marker(self, root: Path) -> None:
        """FR-1.0.1：skill.md（全小写）legacy 兼容识别。"""
        d = root / "legacy-skill"
        d.mkdir()
        (d / "skill.md").write_bytes(b"---\nname: l\n---\n")
        skills = scan_root(root, _spec(root), frozenset())
        assert [s.rel_path for s in skills] == ["legacy-skill"]

    def test_readme_not_marker_and_not_short_circuit(self, root: Path) -> None:
        """06 A2：README/CLAUDE.md 绝不算标记；分类目录下的真 Skill 必须仍被扫到。"""
        cat = root / "research"
        cat.mkdir()
        (cat / "README.md").write_bytes(b"# category readme\n")
        _make_skill(cat, "real-skill")
        skills = scan_root(root, _spec(root), frozenset())
        assert [s.rel_path for s in skills] == ["research/real-skill"]

    def test_nested_subskills(self, root: Path) -> None:
        """FR-1.0.3：subskills/<name>/SKILL.md 计入清单且 is_nested=True。"""
        outer = _make_skill(root, "outer")
        sub = outer / "subskills" / "inner"
        sub.mkdir(parents=True)
        (sub / "SKILL.md").write_bytes(b"---\nname: inner\n---\n")
        skills = scan_root(root, _spec(root), frozenset())
        by_rel = {s.rel_path: s for s in skills}
        assert "outer" in by_rel
        assert by_rel["outer"].is_nested is False
        assert "outer/subskills/inner" in by_rel
        assert by_rel["outer/subskills/inner"].is_nested is True

    def test_non_skill_dirs_skipped(self, root: Path) -> None:
        _make_skill(root, "real")
        for junk in ("node_modules/pkg", ".runtime/store", "__pycache__"):
            d = root / junk
            d.mkdir(parents=True)
            (d / "SKILL.md").write_bytes(b"---\nname: junk\n---\n")
        skills = scan_root(root, _spec(root), frozenset())
        assert [s.rel_path for s in skills] == ["real"]


class TestScanOutput:
    def test_meta_parsed(self, root: Path) -> None:
        body = "---\nname: beta-name\ndescription: 描述\n---\n"
        _make_skill(root, "beta", body.encode("utf-8"))
        skills = scan_root(root, _spec(root), frozenset())
        assert skills[0].meta is not None
        assert skills[0].meta.name == "beta-name"

    def test_meta_none_on_bad_frontmatter(self, root: Path) -> None:
        """单个坏文件降级继续（04 §6.1），不中断全量扫描。"""
        d = root / "bad"
        d.mkdir()
        (d / "SKILL.md").write_bytes(b"---\nname: [unclosed\n---\n")
        _make_skill(root, "good")
        skills = scan_root(root, _spec(root), frozenset())
        assert len(skills) == 2
        assert next(s for s in skills if s.rel_path == "bad").meta is None

    def test_pack_hash_stable_and_content_sensitive(self, root: Path) -> None:
        _make_skill(root, "one", b"---\nname: one\n---\nA\n")
        a = scan_root(root, _spec(root), frozenset())[0].pack_hash
        b = scan_root(root, _spec(root), frozenset())[0].pack_hash
        assert a == b  # 同内容两次扫描一致（纯函数口径）

        (root / "one" / "SKILL.md").write_bytes(b"---\nname: one\n---\nB\n")
        c = scan_root(root, _spec(root), frozenset())[0].pack_hash
        assert a != c

    def test_pack_hash_excludes_pyc(self, root: Path) -> None:
        """06 A3（变异验证）：__pycache__/*.pyc 生成不改变 pack_hash ——
        不排除则每次使用后部署副本永久显示"已变更"。"""
        _make_skill(root, "py", b"---\nname: py\n---\n")
        before = scan_root(root, _spec(root), frozenset())[0].pack_hash
        cache = root / "py" / "__pycache__"
        cache.mkdir()
        (cache / "mod.cpython-311.pyc").write_bytes(b"\x00junk")
        after = scan_root(root, _spec(root), frozenset())[0].pack_hash
        assert before == after

    def test_runtime_key_scope_propagated(self, root: Path) -> None:
        _make_skill(root, "x")
        spec = RootSpec(runtime_key="workbuddy", root=root, scope="project")
        skills = scan_root(root, spec, frozenset())
        assert skills[0].runtime_key == "workbuddy"
        assert skills[0].scope == "project"


class TestScanRootsMerge:
    def test_canonical_path_dedup(self, tmp_path: Path) -> None:
        """07 N9（变异验证）：两个 Runtime 根指向同一物理目录（软链/同目录）
        必须按 resolve() 去重 —— 否则同一 Skill 被扫成两条，产生假 UNMANAGED。"""
        real = tmp_path / "real-skills"
        real.mkdir()
        _make_skill(real, "dup")
        alias = tmp_path / "alias-skills"
        try:
            alias.symlink_to(real)
        except OSError:
            pytest.skip("本机无 symlink 权限（Windows 非开发者模式）")
        specs = [
            RootSpec("claude", real, "user"),
            RootSpec("agents", alias, "user"),
        ]
        skills = scan_roots(specs)
        # 同一物理目录合并为一份产出（归属第一个 Runtime）
        assert len(skills) == 1
        assert skills[0].runtime_key == "claude"

    def test_same_root_twice(self, root: Path) -> None:
        _make_skill(root, "y")
        skills = scan_roots([_spec(root), _spec(root)])
        assert len(skills) == 1


@pytest.mark.skipif(
    sys.platform == "win32" and not os.environ.get("CI"),
    reason="Windows symlink 权限不稳定",
)
class TestSymlinkNotFollowed:
    def test_symlink_dir_not_followed(self, tmp_path: Path) -> None:
        """防环：符号链接目录不跟随。"""
        outside = tmp_path / "outside"
        _make_skill(outside, "outside-skill")
        root = tmp_path / "rt"
        root.mkdir()
        try:
            (root / "link").symlink_to(outside)
        except OSError:
            pytest.skip("无 symlink 权限")
        skills = scan_root(root, _spec(root), frozenset())
        assert [s.rel_path for s in skills] == []
