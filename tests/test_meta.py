"""协作元数据版本化 + 重建守卫单测（04 §2.3 / FR-1.0.30 / 06 A8）。

坑（06 A8，变异验证）：元数据损坏或首次克隆时，"照元数据重建"会把整库
清空（元数据空 → 重建出空库 → 再写回覆盖）。守卫去掉后本测试变红。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skillos.core.fs.meta import (
    META_DIR,
    MetaIndex,
    MetaIndexMismatch,
    SkillMetadata,
    rebuild_guard,
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _entry(slug: str, tags: list[str] | None = None, enabled: bool = True) -> SkillMetadata:
    return SkillMetadata(slug=slug, tags=tags or [], enabled=enabled, profile=None)


class TestReadWrite:
    def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        idx = MetaIndex(repo)
        idx.write({"a": _entry("a", ["coding"]), "b": _entry("b", ["review"], enabled=False)})
        loaded = MetaIndex(repo).read()
        assert loaded["a"].tags == ["coding"]
        assert loaded["b"].enabled is False

    def test_written_into_repo_meta_dir(self, tmp_path: Path) -> None:
        """元数据必须进 <repo>/.skillos/（随 git 同步），不是 ~/.skillos。"""
        repo = _repo(tmp_path)
        MetaIndex(repo).write({"a": _entry("a")})
        assert (repo / META_DIR / "meta.json").is_file()

    def test_deterministic_bytes(self, tmp_path: Path) -> None:
        """两次写同样数据字节相同 —— git diff 干净（元数据是仓库一部分）。"""
        repo = _repo(tmp_path)
        entries = {"b": _entry("b"), "a": _entry("a", ["x"])}
        MetaIndex(repo).write(entries)
        first = (repo / META_DIR / "meta.json").read_bytes()
        MetaIndex(repo).write(entries)
        second = (repo / META_DIR / "meta.json").read_bytes()
        assert first == second

    def test_read_empty_repo(self, tmp_path: Path) -> None:
        assert MetaIndex(_repo(tmp_path)).read() == {}

    def test_corrupted_json_raises(self, tmp_path: Path) -> None:
        """损坏不静默兜底（CODEBUDDY §7）—— 显式抛错，由上层决定全量扫描。"""
        import json

        repo = _repo(tmp_path)
        (repo / META_DIR).mkdir()
        (repo / META_DIR / "meta.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            MetaIndex(repo).read()

    def test_count(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        idx = MetaIndex(repo)
        idx.write({"a": _entry("a"), "b": _entry("b")})
        assert idx.skill_count() == 2


class TestRebuildGuard:
    """重建守卫（FR-1.0.30）：META_INDEX_MISMATCH 不得重建。"""

    def test_meta_zero_repo_has_dirs_rejected(self) -> None:
        with pytest.raises(MetaIndexMismatch):
            rebuild_guard(meta_count=0, repo_skill_dirs=3)

    def test_meta_zero_repo_zero_ok(self) -> None:
        rebuild_guard(meta_count=0, repo_skill_dirs=0)  # 真空仓库

    def test_meta_matches_repo_ok(self) -> None:
        rebuild_guard(meta_count=3, repo_skill_dirs=3)

    def test_meta_nonzero_repo_zero_allowed(self) -> None:
        """规格（06 A8）只拒绝"元数据 0 个而仓库有目录"；
        反向不一致（如浅克隆/部分检出的过渡态）放行，由全量扫描修正。"""
        rebuild_guard(meta_count=3, repo_skill_dirs=0)


class TestScanExclusion:
    def test_meta_dir_excluded_from_content_enumeration(self, tmp_path: Path) -> None:
        """.skillos/ 必须被 scan 排除，否则元数据目录被当成 Skill（排期 P1）。"""
        from skillos.core.algorithm.hash import is_excluded

        assert is_excluded(".skillos/meta.json")
