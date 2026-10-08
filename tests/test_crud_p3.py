"""P3 CRUD：新建 Skill（FR-2.1.7）+ 删除守卫（FR-1.0.26 ②）。

- create：canonical 模板落盘 + git commit + 索引；slug 冲突（含大小写归一化，
  NFR-7.4）与非法 slug 一律拒绝 —— 写操作不猜用户意图。
- delete：canonical 会删内容必须先逐条确认（红线 16）；runtime 磁盘绝不静默删。
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
    for rt in ("claude", "agents"):
        d = home / f".{rt}" / "skills" / "demo"
        (d / "references").mkdir(parents=True)
        (d / "SKILL.md").write_bytes(_SKILL_MD)
        (d / "references" / "guide.md").write_bytes(b"guide\n")
    paths = EnvPaths(repo_root=tmp_path / "repo", db_path=tmp_path / "state" / "skillos.db")
    engine = LocalEngine(paths, home=home)
    return engine, home


class TestCreateSkill:
    """FR-2.1.7 新建入口（引擎侧）。"""

    def test_create_writes_canonical_and_index(self, env) -> None:
        engine, _ = env
        engine.init_env()
        result = engine.create_skill(
            "brand-new", name="Brand New", description="x" * 40
        )
        skill_md = engine.paths.repo_root / "skills" / "brand-new" / "SKILL.md"
        assert skill_md.exists()
        assert result["commit"]
        assert result["gate_has_error"] is False
        row = engine.db.get_skill_by_slug("brand-new")
        assert row is not None and row.name == "Brand New"
        assert row.description == "x" * 40
        # 未分发：无 binding，首次 push 时建立（04 §2.2.5 同源语义）
        assert engine.db.list_bindings(row.id) == []

    def test_created_frontmatter_parses_and_gate_clean(self, env) -> None:
        """模板必须产出可解析的 frontmatter（含中文 description 的 YAML 安全转义）。"""
        engine, _ = env
        engine.init_env()
        engine.create_skill(
            "brand-new", name="Brand New",
            description="一个全新的技能描述，包含冒号: 与引号\"的混排",
        )
        snap = engine.git.snapshot_at(engine._head(), "brand-new")
        assert snap.meta.name == "Brand New"
        assert "冒号" in (snap.meta.description or "")
        gate = engine.gate_of("brand-new")
        assert not gate.has_error

    def test_create_slug_conflict_rejected(self, env) -> None:
        engine, _ = env
        engine.init_env()
        engine.import_skills()
        with pytest.raises(ValueError, match="SLUG_EXISTS"):
            engine.create_skill("demo", name="demo", description="x" * 40)

    def test_create_case_collision_rejected(self, env) -> None:
        """NFR-7.4：大小写变体不得并存。'Demo' 先被 kebab 校验拦下（SLUG_INVALID）；
        create 内的 path_key 归一化比对是纵深防御——kebab 限制 ASCII 小写后
        大小写冲突到不了那里，但非 ASCII 归一化差异仍以 path_key 为唯一判据
        （RED LINE 21：路径键归一化必须走单一 path_key()）。"""
        engine, _ = env
        engine.init_env()
        engine.import_skills()
        with pytest.raises(ValueError, match="SLUG_INVALID"):
            engine.create_skill("Demo", name="demo", description="x" * 40)

    def test_create_invalid_slug_rejected(self, env) -> None:
        engine, _ = env
        engine.init_env()
        with pytest.raises(ValueError, match="SLUG_INVALID"):
            engine.create_skill("My Skill", name="x", description="x" * 40)
        with pytest.raises(ValueError, match="SLUG_INVALID"):
            engine.create_skill("foo_bar", name="x", description="x" * 40)

    def test_create_empty_description_rejected(self, env) -> None:
        """fm.description.required 是 error 级门禁：创建即违规的 Skill 没有意义。"""
        engine, _ = env
        engine.init_env()
        with pytest.raises(ValueError, match="VALIDATION"):
            engine.create_skill("brand-new", name="x", description="")

    def test_create_then_push_synced(self, env) -> None:
        """闭环：新建 → push 四端 SYNCED（模板产物必须可直接分发）。"""
        engine, _ = env
        engine.init_env()
        engine.create_skill("brand-new", name="Brand New", description="x" * 40)
        report = engine.push_skill("brand-new")
        assert report.ok, [(o.error_code, o.detail) for o in report.outcomes]
        assert all(o.wrote for o in report.outcomes)
        row = engine.db.get_skill_by_slug("brand-new")
        assert all(
            c.state == DriftState.SYNCED
            for c in engine.status_matrix()
            if c.skill_id == row.id
        )


class TestTaskExecutorCreate:
    """skill_create 经白名单执行器（WS 通道同路径，与 Web/API/CLI 同一入口）。"""

    def test_skill_create_routes_to_engine(self, env) -> None:
        from skillos.daemon.main import TaskExecutor

        engine, _ = env
        engine.init_env()
        ex = TaskExecutor(engine)
        r = ex.execute("skill_create", {
            "slug": "via-ws", "name": "Via WS", "description": "x" * 40,
        })
        assert r["ok"] is True
        assert r["data"]["slug"] == "via-ws"
        assert engine.db.get_skill_by_slug("via-ws") is not None

    def test_skill_create_slug_exists_maps_code(self, env) -> None:
        """SLUG_EXISTS 必须原样透传到信封 code（前端按 code 分支提示）。"""
        from skillos.daemon.main import TaskExecutor

        engine, _ = env
        engine.init_env()
        engine.import_skills()
        ex = TaskExecutor(engine)
        r = ex.execute("skill_create", {
            "slug": "demo", "name": "demo", "description": "x" * 40,
        })
        assert r["ok"] is False
        assert r["error"]["code"] == "SLUG_EXISTS"


class TestDeleteSkill:
    """删除（P3 CRUD）。红线 16：会删内容必须先列出并等确认；runtime 磁盘绝不静默删。

    删除口径：只删 canonical + 索引（skill 记录 + bindings）；
    runtime 磁盘目录保留（数据安全，处置权在用户——后续 scan 会以
    未纳管目录出现，可 re-import 或手动清理）。
    """

    def test_delete_dry_run_lists_removals(self, env) -> None:
        engine, _ = env
        engine.init_env()
        engine.import_skills()
        preview = engine.delete_skill("demo", dry_run=True)
        assert preview["ok"] is True and preview["dry_run"] is True
        assert sorted(preview["removals"]) == ["SKILL.md", "references/guide.md"]
        # dry_run 不动真值（canonical / 索引原样）
        assert (engine.paths.repo_root / "skills" / "demo" / "SKILL.md").exists()
        assert engine.db.get_skill_by_slug("demo") is not None

    def test_delete_without_confirm_rejected(self, env) -> None:
        """FR-1.0.26 ②：未逐条确认前整体拒绝（与 rollback 同判据，FR-1.3.7）。"""
        engine, _ = env
        engine.init_env()
        engine.import_skills()
        r = engine.delete_skill("demo", dry_run=False)
        assert r["ok"] is False
        assert r["error_code"] == "REMOVALS_UNCONFIRMED"
        assert r["removals"]
        assert engine.db.get_skill_by_slug("demo") is not None  # 索引未动
        assert (engine.paths.repo_root / "skills" / "demo" / "SKILL.md").exists()

    def test_delete_confirmed_removes_canonical_and_index(self, env) -> None:
        engine, _ = env
        engine.init_env()
        engine.import_skills()
        sid = engine.db.get_skill_by_slug("demo").id
        assert engine.db.list_bindings(sid)  # 前置：有 binding
        removals = engine.delete_skill("demo", dry_run=True)["removals"]
        r = engine.delete_skill("demo", dry_run=False, confirmed_removals=removals)
        assert r["ok"] is True and r["commit"]
        assert not (engine.paths.repo_root / "skills" / "demo").exists()
        assert engine.db.get_skill_by_slug("demo") is None
        assert engine.db.list_bindings(sid) == []  # bindings 级联清理
        # 溯源仍在（FR-1.0.22）：git log 按 path 可查到 delete commit
        commits = engine.git.log(path="skills/demo", limit=20)
        assert any("delete demo" in c.message for c in commits)

    def test_delete_runtime_disk_untouched(self, env) -> None:
        """红线 16：runtime 磁盘绝不静默删。删除后目录保留，处置权在用户。"""
        engine, home = env
        engine.init_env()
        engine.import_skills()
        removals = engine.delete_skill("demo", dry_run=True)["removals"]
        engine.delete_skill("demo", dry_run=False, confirmed_removals=removals)
        for rt in ("claude", "agents"):
            assert (home / f".{rt}" / "skills" / "demo" / "SKILL.md").exists()

    def test_delete_unknown_slug_rejected(self, env) -> None:
        engine, _ = env
        engine.init_env()
        with pytest.raises(ValueError, match="NOT_FOUND"):
            engine.delete_skill("ghost")

    def test_delete_stray_file_rejected_not_guessed(self, env) -> None:
        """红线 16 变异验证：canonical 目录出现 HEAD 之外的文件（外部手动放入）
        → 报 RESIDUE 列出差异，绝不静默连它一起删。删掉该守卫本测试必变红。"""
        engine, _ = env
        engine.init_env()
        engine.import_skills()
        (engine.paths.repo_root / "skills" / "demo" / "stray.txt").write_bytes(b"x")
        r = engine.delete_skill("demo", dry_run=True)
        assert r["ok"] is False
        assert r["error_code"] == "RESIDUE"
        assert r["removals"] == ["stray.txt"]
        # 未确认前什么都不动
        assert (engine.paths.repo_root / "skills" / "demo" / "stray.txt").exists()

    def test_delete_created_skill_end_to_end(self, env) -> None:
        """新建的 Skill 同样可删（create→delete 闭环，索引与 canonical 归零）。"""
        engine, _ = env
        engine.init_env()
        engine.create_skill("short-lived", name="Short", description="x" * 40)
        removals = engine.delete_skill("short-lived", dry_run=True)["removals"]
        assert removals == ["SKILL.md"]
        r = engine.delete_skill("short-lived", dry_run=False, confirmed_removals=removals)
        assert r["ok"] is True
        assert engine.db.get_skill_by_slug("short-lived") is None
        assert not (engine.paths.repo_root / "skills" / "short-lived").exists()


class TestTaskExecutorDelete:
    """skill_delete 经白名单执行器（WS 通道同路径）。"""

    def test_skill_delete_dry_run_routes_to_engine(self, env) -> None:
        from skillos.daemon.main import TaskExecutor

        engine, _ = env
        engine.init_env()
        engine.import_skills()
        ex = TaskExecutor(engine)
        r = ex.execute("skill_delete", {"slug": "demo", "dry_run": True})
        assert r["ok"] is True
        assert sorted(r["data"]["removals"]) == ["SKILL.md", "references/guide.md"]

    def test_skill_delete_unconfirmed_maps_removals(self, env) -> None:
        """REMOVALS_UNCONFIRMED + removals 列表必须进信封（前端复用 RemovalsConfirmDialog）。"""
        from skillos.daemon.main import TaskExecutor

        engine, _ = env
        engine.init_env()
        engine.import_skills()
        ex = TaskExecutor(engine)
        r = ex.execute("skill_delete", {"slug": "demo", "dry_run": False})
        assert r["ok"] is False
        assert r["error"]["code"] == "REMOVALS_UNCONFIRMED"
        assert r["removals"]

    def test_skill_delete_confirmed_ok(self, env) -> None:
        from skillos.daemon.main import TaskExecutor

        engine, _ = env
        engine.init_env()
        engine.import_skills()
        ex = TaskExecutor(engine)
        removals = ex.execute("skill_delete", {"slug": "demo", "dry_run": True})["data"]["removals"]
        r = ex.execute("skill_delete", {
            "slug": "demo", "dry_run": False, "confirm_removals": removals,
        })
        assert r["ok"] is True
        assert engine.db.get_skill_by_slug("demo") is None
