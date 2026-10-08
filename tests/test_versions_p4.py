"""P4 版本管理：回滚 / tag / 文件级历史。

验证口径：
- FR-1.0.10：rollback 前自动建安全快照（快照可见、可再回滚）
- FR-1.0.22：回滚 commit 即溯源（author + 目标版本进 git 历史，不可篡改）+ NFR-9 sync_records
- FR-1.0.26 ②：回滚使文件消失 → dry_run 列出；未确认整体拒绝（不是 held_back）
- FR-1.3.7：dry_run 预览与真跑同判据
- tag：附注 tag 创建 / 清单（解引用 commit sha）
- 文件级历史：--follow 追踪
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skillos.services.engine import LocalEngine, default_env

_SKILL_MD = (
    b"---\nname: demo\nversion: 1.0.0\n"
    b"description: a demo skill doing useful everyday work\n---\nbody v1\n"
)


@pytest.fixture()
def engine(tmp_path: Path) -> LocalEngine:
    home = tmp_path / "home"
    d = home / ".claude" / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(_SKILL_MD)
    e = LocalEngine(default_env(home), home=home)
    e.init_env()
    e.import_skills()
    return e


def _write(e: LocalEngine, slug: str, rel: str, content: bytes, msg: str) -> str:
    tree = e.file_tree(slug)
    base = next((f["hash"] for f in tree if f["path"] == rel), "")
    return e.write_file(slug, rel, content, base_hash=base, message=msg)


def test_rollback_restores_content_and_writes_provenance(engine: LocalEngine):
    """回滚恢复旧内容；commit 与 sync_records 均含溯源（FR-1.0.22 / NFR-9）。"""
    v1 = engine.head()
    _write(engine, "demo", "SKILL.md",
           _SKILL_MD.replace(b"body v1", b"body v2"), "edit to v2")
    v2 = engine.head()
    assert v1 != v2

    r = engine.rollback_skill("demo", v1, dry_run=False,
                              author="alice <alice@example.com>")
    assert r["ok"] and r["commit"]
    snap = engine.git.snapshot_at(engine.head(), "demo")
    assert snap.files[0].content == _SKILL_MD  # 内容已回到 v1

    # 溯源在 git 历史：rollback commit 的 author 是操作者（不是引擎默认值）
    log = engine.git.log(path="skills/demo", limit=5)
    rb = log[0]
    assert rb.author == "alice"
    assert v1 in rb.message and "rollback" in rb.message

    # NFR-9：sync_records 落了 rollback 记录
    from skillos.store.models import SyncRecord

    recs = engine.db._s.query(SyncRecord).filter_by(action="rollback").all()  # type: ignore[attr-defined]
    assert len(recs) == 1
    assert recs[0].by == "alice <alice@example.com>"
    assert recs[0].detail["target"] == v1


def test_rollback_snapshot_before_destructive_change(engine: LocalEngine):
    """FR-1.0.10：回滚前自动快照，v2 内容不丢、可再回滚找回。"""
    _write(engine, "demo", "SKILL.md",
           _SKILL_MD.replace(b"body v1", b"body v2 precious"), "precious edit")
    v2_sha = engine.head()
    r = engine.rollback_skill("demo", "HEAD~1", dry_run=False)
    assert r["ok"]
    snap = engine.git.snapshot_at(engine.head(), "demo")
    assert snap.files[0].content == _SKILL_MD  # 回到 v1

    # 快照 commit 仍在历史里（可见），再回滚到 v2 可找回
    r2 = engine.rollback_skill("demo", v2_sha, dry_run=False)
    assert r2["ok"]
    snap2 = engine.git.snapshot_at(engine.head(), "demo")
    assert b"precious" in snap2.files[0].content


def test_rollback_dry_run_lists_removals_and_requires_confirmation(engine: LocalEngine):
    """FR-1.0.26 ② + FR-1.3.7：dry_run 列出将消失路径；未确认 → 整体拒绝。"""
    v1 = engine.head()
    _write(engine, "demo", "extra.md", b"extra file\n", "add extra")
    v2 = engine.head()

    # 预览：回滚到 v1 将使 extra.md 消失
    preview = engine.rollback_skill("demo", v1)
    assert preview["ok"] and preview["dry_run"] is True
    assert preview["removals"] == ["extra.md"]
    assert preview["changed"] == []
    head_before = engine.head()
    assert engine.file_tree("demo") and len(engine.file_tree("demo")) == 2  # 未落盘

    # 真跑但未确认 → 整体拒绝（不 held_back：回滚要完整一致状态）
    r = engine.rollback_skill("demo", v1, dry_run=False)
    assert not r["ok"] and r["error_code"] == "REMOVALS_UNCONFIRMED"
    assert r["removals"] == ["extra.md"]
    assert engine.head() == head_before  # 未动

    # 确认后成功，extra.md 消失
    r = engine.rollback_skill("demo", v1, dry_run=False, confirmed_removals=["extra.md"])
    assert r["ok"] and r["removals"] == ["extra.md"]
    assert v2 != engine.head()
    paths = [f["path"] for f in engine.file_tree("demo")]
    assert "extra.md" not in paths


def test_rollback_noop_and_invalid_inputs(engine: LocalEngine):
    v1 = engine.head()
    r = engine.rollback_skill("demo", v1, dry_run=False)
    assert r["ok"] and r.get("noop") is True

    with pytest.raises(ValueError, match="未纳管"):
        engine.rollback_skill("ghost", v1, dry_run=False)
    with pytest.raises(ValueError, match="无法解析"):
        engine.rollback_skill("demo", "no-such-ref", dry_run=False)


def test_tags_create_and_list_with_deref(engine: LocalEngine):
    """附注 tag：创建后清单给出解引用 commit sha（时间轴徽标可对齐）。"""
    head = engine.head()
    assert engine.list_tags() == []
    r = engine.create_tag("v1.0", "first tag", author="bob <bob@example.com>")
    assert r["ok"]
    tags = engine.list_tags()
    assert len(tags) == 1
    assert tags[0]["name"] == "v1.0" and tags[0]["sha"] == head  # 解引用到 commit

    with pytest.raises(ValueError, match="非法 tag 名"):
        engine.create_tag("bad/name")


def test_file_history_follow(engine: LocalEngine):
    """文件级历史：按路径过滤出该文件的提交序列。"""
    _write(engine, "demo", "SKILL.md",
           _SKILL_MD.replace(b"body v1", b"body v2"), "edit skill only")
    _write(engine, "demo", "notes.md", b"notes\n", "add notes")
    hist = engine.file_history("demo", "SKILL.md")
    messages = [h["message"] for h in hist]
    assert "edit skill only" in messages
    assert "add notes" not in messages  # 只含该文件的提交
    with pytest.raises(ValueError, match="未纳管"):
        engine.file_history("ghost", "SKILL.md")


# ------------------------------------------------ daemon 任务执行器层


class TestTaskExecutorP4:
    """rollback / file_history / tag 经白名单执行器（WS 通道同路径）。"""

    def test_rollback_dry_run_then_confirmed(self, engine: LocalEngine):
        from skillos.daemon.main import TaskExecutor

        ex = TaskExecutor(engine)
        v1 = engine.head()
        _write(engine, "demo", "extra.md", b"extra\n", "add extra")

        r = ex.execute("rollback", {"slug": "demo", "ref": v1, "dry_run": True})
        assert r["ok"] and r["data"]["removals"] == ["extra.md"]

        r = ex.execute("rollback", {"slug": "demo", "ref": v1})  # 缺省 dry_run=true → 预览
        assert r["ok"] and r["data"]["dry_run"] is True

        r = ex.execute("rollback", {"slug": "demo", "ref": v1, "dry_run": False})
        assert not r["ok"] and r["error"]["code"] == "REMOVALS_UNCONFIRMED"
        assert r["removals"] == ["extra.md"]

        r = ex.execute("rollback", {"slug": "demo", "ref": v1, "dry_run": False,
                                    "confirm_removals": ["extra.md"]})
        assert r["ok"] and r["data"]["commit"]
        assert "extra.md" not in [f["path"] for f in engine.file_tree("demo")]

    def test_file_history_and_tags_via_executor(self, engine: LocalEngine):
        from skillos.daemon.main import TaskExecutor

        ex = TaskExecutor(engine)
        r = ex.execute("file_history", {"slug": "demo", "path": "SKILL.md"})
        assert r["ok"] and len(r["data"]["items"]) >= 1

        r = ex.execute("tag_create", {"name": "v-p4", "message": "p4 tag"})
        assert r["ok"]
        r = ex.execute("tag_list", {})
        assert [t["name"] for t in r["data"]["items"]] == ["v-p4"]
        assert r["data"]["items"][0]["sha"] == engine.head()

        # 非白名单 / 非法输入
        assert not ex.execute("tag_delete", {}).get("ok")
        r = ex.execute("file_history", {"slug": "ghost", "path": "SKILL.md"})
        assert not r["ok"] and r["error"]["code"] == "INVALID_INPUT"

    def test_pull_via_executor(self, engine: LocalEngine):
        """P5 遗留接线：daemon pull 走 engine.pull_skill（锁在引擎内，无死锁）。

        注：disk 与 canonical 字节不等是 render_layout 的 by design（FR-1.0.27
        自证以 installed_hash 为锚，不要求与 git bytes 一致）—— 测试只验接口契约。
        """
        from skillos.daemon.main import TaskExecutor

        ex = TaskExecutor(engine)
        r = ex.execute("push", {"skill_ids": ["demo"]})
        assert r["ok"]
        # 正常调用：返回结构（无论 noop/有差异都是合法）
        r = ex.execute("pull", {"slug": "demo", "runtime": "claude"})
        assert r["ok"] and "changed" in r["data"] and "removed" in r["data"]
        # 未知 runtime / 未纳管 skill → 结构化 INVALID_INPUT
        assert ex.execute("pull", {"slug": "demo", "runtime": "no-such"}).get("ok") is False
        assert ex.execute("pull", {"slug": "ghost", "runtime": "claude"}).get("ok") is False
