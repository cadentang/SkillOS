"""hash 两级哈希单测（04 §2.1.3 哈希口径，FR-1.0.5 / T16）。

变异验证（03 §8.2）：test_byte_level_* 系列是 FR-1.0.5 的反向验证 ——
把实现改回「归一化后进哈希」的旧口径，这些测试必须变红。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skillos.core.algorithm.hash import (
    aggregate_entries,
    aggregate_hash,
    compare_normalized,
    hash_bytes,
    is_excluded,
    is_foldable_text,
    list_content_files,
)
from skillos.core.model import FileEntry


class TestHashBytes:
    """第一级：字节级、绝不归一化（存储身份）。"""

    def test_byte_level_no_normalization(self):
        """坑：07 D6 / 06 B1 —— 旧口径"归一化后进哈希"会让"永远可更新、
        两端来回互推"成为常态；CRLF 与 LF 字节不同就必须判不等。"""
        assert hash_bytes(b"a\r\nb\r\n") != hash_bytes(b"a\nb\n")

    def test_same_bytes_same_hash(self):
        assert hash_bytes(b"hello") == hash_bytes(b"hello")

    def test_bom_differs(self):
        assert hash_bytes(b"\xef\xbb\xbfabc") != hash_bytes(b"abc")


class TestAggregate:
    def test_path_participates(self):
        assert aggregate_hash({"a.md": b"1"}) != aggregate_hash({"b.md": b"1"})

    def test_order_independent(self):
        a = aggregate_hash({"x.md": b"1", "y.md": b"2"})
        b = aggregate_hash({"y.md": b"2", "x.md": b"1"})
        assert a == b

    def test_posix_separator(self):
        assert aggregate_hash({"a/b.md": b"1"}) == aggregate_hash({"a\\b.md": b"1"})

    def test_entries_skip_untracked_assets(self):
        """FR-1.0.20：in_git=False 的大文件不参与漂移哈希。"""
        managed = FileEntry(path="SKILL.md", content=b"x")
        asset = FileEntry(path="big.bin", content=b"y", in_git=False)
        assert aggregate_entries([managed, asset]) == aggregate_entries([managed])

    def test_entries_crlf_not_folded(self):
        a = FileEntry(path="SKILL.md", content=b"a\r\nb")
        b = FileEntry(path="SKILL.md", content=b"a\nb")
        assert aggregate_entries([a]) != aggregate_entries([b])


class TestCompareNormalized:
    """第二级兜底比较：只在字节 hash 已不等时调用，仅用于"是否仅风格差异"提示。"""

    def test_crlf_style_only(self):
        assert compare_normalized(b"a\r\nb\r\n", b"a\nb\n") is True

    def test_bom_style_only(self):
        assert compare_normalized(b"\xef\xbb\xbfabc", b"abc") is True

    def test_trailing_whitespace_style_only(self):
        assert compare_normalized(b"a  \nb\t\n", b"a\nb\n") is True

    def test_nfc_style_only(self):
        composed = "café".encode()
        decomposed = "cafe\u0301".encode("utf-8")
        assert compare_normalized(composed, decomposed) is True

    def test_isolated_cr_not_folded(self):
        """06 B1：孤立 \\r 可能是脚本里有意义的字节，不作行尾折叠。"""
        assert compare_normalized(b"a\rb\n", b"a\nb\n") is False

    def test_binary_never_folded(self):
        assert compare_normalized(b"\x00\x01\x02", b"\x00\x01\x03") is False

    def test_real_difference_detected(self):
        assert compare_normalized(b"hello world", b"hello agent") is False


class TestIsFoldableText:
    def test_utf8_text(self):
        assert is_foldable_text("---\nname: 中文\n---\n".encode()) is True

    def test_nul_rejected(self):
        assert is_foldable_text(b"abc\x00def") is False

    def test_invalid_utf8_rejected(self):
        assert is_foldable_text(b"\xff\xfe\xfa") is False


class TestExclusions:
    """排除集三类（FR-1.0.5）：① 影响 hash ② 只影响兜底比较 ③ 纯噪音。

    坑：06 A3 —— __pycache__/*.pyc 不排除，每次使用后部署副本永久显示"已变更"。
    """

    def test_hash_excludes(self):
        for rel in (
            "s/__pycache__/x.cpython-313.pyc",
            "s/mod.pyc",
            "s/.DS_Store",
            ".git/config",
            ".skillos/meta.json",
            "s/node_modules/pkg/index.js",
            "s/.runtime/marketplace/x.md",
            "s/.venv/lib/py.py",
        ):
            assert is_excluded(rel), rel

    def test_noise_excludes(self):
        for rel in (
            "s/run.log",
            "s/.tmp-abc123/file.md",
            "s/.staged-uuid/file.md",
            "s/backup.old",
        ):
            assert is_excluded(rel), rel

    def test_normal_files_not_excluded(self):
        for rel in ("SKILL.md", "scripts/run.py", "references/a.md", "subskills/x/SKILL.md"):
            assert not is_excluded(rel), rel


class TestListContentFiles:
    """唯一枚举入口（FR-1.0.5）：scan / hash / diff / removals 守卫共用，
    禁止各自 filter —— 范围漂移是上游多处误报的根因。"""

    def _setup(self, root: Path) -> None:
        (root / "skill-a").mkdir(parents=True)
        (root / "skill-a" / "SKILL.md").write_bytes(b"---\nname: a\n---\n")
        (root / "skill-a" / "scripts").mkdir()
        (root / "skill-a" / "scripts" / "run.py").write_bytes(b"print(1)\n")
        (root / "skill-a" / "scripts" / "__pycache__").mkdir()
        (root / "skill-a" / "scripts" / "__pycache__" / "run.pyc").write_bytes(b"\x00\x01")
        (root / "skill-a" / ".DS_Store").write_bytes(b"\x00")
        (root / "skill-a" / "big.bin").write_bytes(b"x" * (5 * 1024 * 1024 + 1))

    def test_enumerates_with_exclusions_and_in_git_flag(self, tmp_path: Path):
        root = tmp_path / "pack"
        self._setup(root)
        entries = list_content_files(root)
        paths = sorted(e.path for e in entries)
        assert paths == ["skill-a/SKILL.md", "skill-a/big.bin", "skill-a/scripts/run.py"]

        by_path = {e.path: e for e in entries}
        assert by_path["skill-a/SKILL.md"].in_git is True
        assert by_path["skill-a/SKILL.md"].content == b"---\nname: a\n---\n"
        # T2：超阈值文件 in_git=False，不参与漂移哈希
        assert by_path["skill-a/big.bin"].in_git is False

    def test_deterministic_order(self, tmp_path: Path):
        root = tmp_path / "pack"
        self._setup(root)
        a = [e.path for e in list_content_files(root)]
        b = [e.path for e in list_content_files(root)]
        assert a == b


class TestNoiseResidue:
    """FR-1.0.15 残留形态必须进噪音集 —— 否则残留 tmp 目录（内含 SKILL.md）
    会被 scan 收编成一个新 Skill，一次中断演变成持续污染（07 N16f）。"""

    def test_tmp_and_staged_dirs_excluded(self):
        for rel in (
            ".demo-skill.tmp-x7f/SKILL.md",   # 分发残留：.<name>.tmp-<rand>/
            "skills.staged-a/SKILL.md",        # 暂存残留
            "skills/demo-skill.old/SKILL.md",  # 两步交换残留
            "s/debug.log",
        ):
            assert is_excluded(rel), rel

    def test_missing_root_raises_not_silent(self, tmp_path: Path):
        """NFR-11.3：无法判断目录内容时必须报错 —— 静默返回空会让
        removals 守卫把「读不了的目录」当「空目录」，漏报会删的文件。"""
        with pytest.raises(FileNotFoundError):
            list_content_files(tmp_path / "no-such-dir")
