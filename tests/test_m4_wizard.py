"""M4-03 新建 Skill 向导后端口径（E-07.1 ~ E-07.6）。

向导的四步里有三步在后端也有对应约束，前端只是"提前提示"：

- name：kebab-case（`SLUG_INVALID`），重名 / 归一化碰撞分开报（`NAME_CONFLICT`
  / `NAME_COLLISION`，E-07.4）；
- description：必填（`VALIDATION`），长度口径交给 gate 的 `fm.description.length`；
- 模板：三选一，未知取值拒绝（`VALIDATION`），且模板自身必须 gate 全绿（E-07.5）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skillos.services.engine import CREATE_TEMPLATES, EnvPaths, LocalEngine

_DEMO = (
    b"---\nname: demo\nversion: 1.0.0\n"
    b"description: a demo skill doing useful everyday work\n---\nbody\n"
)
_DESC = "x" * 40


@pytest.fixture()
def env(tmp_path: Path):
    home = tmp_path / "home"
    d = home / ".claude" / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(_DEMO)
    engine = LocalEngine(
        EnvPaths(repo_root=tmp_path / "repo", db_path=tmp_path / "state" / "skillos.db"),
        home=home,
    )
    engine.init_env()
    return engine


class TestNameValidation:
    def test_kebab_required(self, env) -> None:
        for bad in ("My Skill", "foo_bar", "Foo", "foo--bar"):
            with pytest.raises(ValueError, match="SLUG_INVALID"):
                env.create_skill(bad, description=_DESC)

    def test_duplicate_is_name_conflict(self, env) -> None:
        env.import_skills()
        with pytest.raises(ValueError, match="NAME_CONFLICT"):
            env.create_skill("demo", description=_DESC)

    def test_case_variant_is_name_collision(self, env, monkeypatch) -> None:
        """E-07.4：仅归一化后相同 → NAME_COLLISION（与"重名"分开，前端提示不同）。

        kebab 校验会先把 `Demo` 拦成 SLUG_INVALID，所以这里直接构造一个归一化同键
        的中文 slug 场景不现实 —— 改为放宽 kebab 判定，验证 create 内的
        path_key 纵深防御仍在（红线 21：路径去重只认 path_key）。
        """
        env.import_skills()
        monkeypatch.setattr(
            "skillos.services.engine.is_kebab", lambda _s: True,
        )
        with pytest.raises(ValueError, match="NAME_COLLISION"):
            env.create_skill("DEMO", description=_DESC)

    def test_missing_description_rejected(self, env) -> None:
        with pytest.raises(ValueError, match="VALIDATION"):
            env.create_skill("brand-new", description="   ")

    def test_unknown_template_rejected(self, env) -> None:
        with pytest.raises(ValueError, match="VALIDATION"):
            env.create_skill("brand-new", description=_DESC, template="fancy")


class TestTemplates:
    def test_templates_are_three(self) -> None:
        assert CREATE_TEMPLATES == ("minimal", "with-deps", "with-scripts")

    def test_minimal_only_skill_md(self, env) -> None:
        env.create_skill("solo", description=_DESC, template="minimal")
        files = sorted(
            p.relative_to(env.paths.repo_root / "skills" / "solo").as_posix()
            for p in (env.paths.repo_root / "skills" / "solo").rglob("*") if p.is_file()
        )
        assert files == ["SKILL.md"]

    def test_with_deps_has_empty_dependencies_and_green_gate(self, env) -> None:
        result = env.create_skill(
            "needs-deps", description=_DESC, template="with-deps", category="ops",
        )
        text = (env.paths.repo_root / "skills" / "needs-deps" / "SKILL.md").read_text(
            encoding="utf-8",
        )
        assert "dependencies: []" in text
        assert "category:" in text
        assert result["gate_has_error"] is False, result["gate"]

    def test_with_scripts_creates_skeleton_dirs_and_green_gate(self, env) -> None:
        result = env.create_skill(
            "with-files", description=_DESC, template="with-scripts",
        )
        root = env.paths.repo_root / "skills" / "with-files"
        assert (root / "references" / "example.md").is_file()
        assert (root / "scripts" / "example.py").is_file()
        assert result["gate_has_error"] is False, result["gate"]


class TestCreateSemantics:
    def test_commit_traces_author_and_no_binding(self, env) -> None:
        """E-07.3：创建即纳管（第一条 commit，author 正确），且不自动分发。"""
        result = env.create_skill(
            "traced", description=_DESC, author="alice <alice@example.com>",
        )
        commits = env.git.log(limit=5, path="skills/traced")
        assert commits and commits[0].sha.startswith(result["commit"][:7])
        assert "alice" in commits[0].author
        row = env.db.get_skill_by_slug("traced")
        assert row is not None
        assert env.db.list_bindings(row.id) == []

    def test_category_lands_in_index(self, env) -> None:
        env.create_skill("categorized", description=_DESC, category="devops")
        row = env.db.get_skill_by_slug("categorized")
        assert row is not None and row.category == "devops"
