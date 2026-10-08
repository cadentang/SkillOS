"""GitRepo 七具名方法单测（04 §2.2.3 / FR-1.0.10 / OUT-14）。

坑（变异验证，07 G1/G2/G5）：
- 不显式 -b main --ref-format=files → 依赖用户全局配置，不同机器仓库结构不同
- 不显式 -c core.autocrlf=false → checkout 改字节，git status 永远显示被改（05 P0-1）
- rollback 不先快照 → 回滚本身丢数据（07 N23）
把实现改回旧行为，对应测试必须变红。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skillos.core.fs.version import GitError, GitRepo

_CRLF_CONTENT = b"---\r\nname: demo\r\ndescription: d\r\n---\r\nbody\r\n"


@pytest.fixture()
def repo(tmp_path: Path) -> GitRepo:
    r = GitRepo(tmp_path)
    r.init()
    return r


def _write_skill(root: Path, slug: str = "demo-skill", content: bytes = _CRLF_CONTENT) -> Path:
    d = root / "skills" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_bytes(content)
    (d / "scripts").mkdir(exist_ok=True)
    (d / "scripts" / "run.py").write_bytes(b"print(1)\n")
    return d


class TestInit:
    def test_explicit_branch_main(self, tmp_path: Path) -> None:
        GitRepo(tmp_path).init()
        head = (tmp_path / ".git" / "HEAD").read_text(encoding="utf-8")
        assert "refs/heads/main" in head

    def test_ref_format_files(self, tmp_path: Path) -> None:
        """07 G1：不吃用户 init.defaultRefFormat —— reftable 格式旧工具链读不了。"""
        GitRepo(tmp_path).init()
        assert (tmp_path / ".git" / "refs" / "heads").is_dir()

    def test_init_idempotent(self, tmp_path: Path) -> None:
        r = GitRepo(tmp_path)
        r.init()
        r.init()  # 二次 init 不报错


class TestCommitLog:
    def test_commit_returns_sha_and_log(self, repo: GitRepo, tmp_path: Path) -> None:
        _write_skill(tmp_path)
        sha = repo.commit(["skills"], "feat: add demo")
        assert len(sha) == 40
        commits = repo.log()
        assert commits[0].sha == sha
        assert "feat: add demo" in commits[0].message
        assert commits[0].author
        assert commits[0].at  # ISO8601

    def test_log_path_filter_and_limit(self, repo: GitRepo, tmp_path: Path) -> None:
        _write_skill(tmp_path, "a-skill")
        repo.commit(["skills"], "first")
        _write_skill(tmp_path, "b-skill")
        repo.commit(["skills"], "second")
        only_b = repo.log(path="skills/b-skill")
        assert len(only_b) == 1
        assert "second" in only_b[0].message
        assert len(repo.log(limit=1)) == 1

    def test_chinese_filename(self, repo: GitRepo, tmp_path: Path) -> None:
        """05 P0：core.quotepath=false —— 中文名不得变八进制转义。"""
        d = tmp_path / "skills" / "中文技能"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_bytes(b"---\nname: x\n---\n")
        repo.commit(["skills"], "cn")
        assert repo.show("HEAD", "skills/中文技能/SKILL.md") == b"---\nname: x\n---\n"


class TestShowBytes:
    def test_crlf_preserved(self, repo: GitRepo, tmp_path: Path) -> None:
        """变异验证（05 P0-1 / T16）：checkout/show 不得改字节 ——
        去掉 core.autocrlf=false 后本测试变红。"""
        _write_skill(tmp_path)
        repo.commit(["skills"], "crlf")
        assert repo.show("HEAD", "skills/demo-skill/SKILL.md") == _CRLF_CONTENT

    def test_binary_content(self, repo: GitRepo, tmp_path: Path) -> None:
        d = tmp_path / "skills" / "bin-skill"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_bytes(b"---\nname: b\n---\n")
        (d / "assets").mkdir()
        (d / "assets" / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01")
        repo.commit(["skills"], "bin")
        assert repo.show("HEAD", "skills/bin-skill/assets/x.png") == b"\x89PNG\r\n\x1a\n\x00\x01"


class TestSnapshotAt:
    def test_snapshot_files_and_meta(self, repo: GitRepo, tmp_path: Path) -> None:
        _write_skill(tmp_path)
        sha = repo.commit(["skills"], "snap")
        snap = repo.snapshot_at(sha, "demo-skill")
        assert snap.slug == "demo-skill"
        assert snap.meta is not None
        assert snap.meta.name == "demo"
        paths = sorted(f.path for f in snap.files)
        assert paths == ["SKILL.md", "scripts/run.py"]
        by_path = {f.path: f for f in snap.files}
        assert by_path["SKILL.md"].content == _CRLF_CONTENT  # 字节级
        assert by_path["SKILL.md"].in_git is True

    def test_snapshot_at_old_commit(self, repo: GitRepo, tmp_path: Path) -> None:
        """T6：历史 commit 的快照必须可精确重建 —— base 不落盘的前提。"""
        _write_skill(tmp_path)
        old_sha = repo.commit(["skills"], "v1")
        (tmp_path / "skills" / "demo-skill" / "SKILL.md").write_bytes(
            b"---\nname: demo-v2\n---\nnew body\n"
        )
        repo.commit(["skills"], "v2")
        snap = repo.snapshot_at(old_sha, "demo-skill")
        assert snap.meta.name == "demo"  # 还是 v1 的内容
        assert snap.files[0].content == _CRLF_CONTENT


class TestDiff:
    def test_diff_between_commits(self, repo: GitRepo, tmp_path: Path) -> None:
        _write_skill(tmp_path)
        sha1 = repo.commit(["skills"], "v1")
        (tmp_path / "skills" / "demo-skill" / "SKILL.md").write_bytes(
            b"---\nname: demo\n---\nchanged\n"
        )
        sha2 = repo.commit(["skills"], "v2")
        text = repo.diff(sha1, sha2, path="skills/demo-skill/SKILL.md")
        assert "changed" in text


class TestRollback:
    def test_rollback_snapshots_first_and_no_rewrite(
        self, repo: GitRepo, tmp_path: Path
    ) -> None:
        """变异验证（07 N23）：rollback 前必须自动快照当前状态（含未提交改动），
        且不重写历史 —— 去掉快照步骤本测试变红。"""
        _write_skill(tmp_path)
        sha1 = repo.commit(["skills"], "v1")
        # 磁盘上手改（未提交）
        skill_md = tmp_path / "skills" / "demo-skill" / "SKILL.md"
        skill_md.write_bytes(b"---\nname: demo\n---\ndirty local edit\n")
        new_sha = repo.rollback(sha1, ["skills/demo-skill/SKILL.md"])

        history = [c.message for c in repo.log()]
        # 历史保持：v1 → 快照（skillos: snapshot before rollback）→ 回滚 commit
        assert any("v1" in m for m in history)
        assert any(m.startswith("skillos: snapshot") for m in history), history
        # dirty 内容必须先被快照保存（可再回滚），不能静默丢失
        dirty = b"---\nname: demo\n---\ndirty local edit\n"
        assert any("dirty local edit" in m for m in history) or any(
            repo.show(c.sha, "skills/demo-skill/SKILL.md") == dirty for c in repo.log()
        )
        # 回滚后工作区 = v1 内容
        assert skill_md.read_bytes() == _CRLF_CONTENT
        # 新 commit 在 HEAD
        assert repo.log()[0].sha == new_sha


class TestErrors:
    def test_show_missing_path_raises(self, repo: GitRepo) -> None:
        with pytest.raises(GitError):
            repo.show("HEAD", "skills/nope/SKILL.md")

    def test_git_not_a_repo(self, tmp_path: Path) -> None:
        with pytest.raises(GitError):
            GitRepo(tmp_path).log()
