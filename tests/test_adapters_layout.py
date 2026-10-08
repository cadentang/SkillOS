"""adapter render_layout + RuntimePaths 单测（04 §2.1.2 / FR-1.0.8 / FR-1.0.25 / T6 / T7 / T12）。

坑（07 D1 / N1~N7）：上游同一份 CHANGELOG 对 Codex 目录给出两次相反结论 ——
"哪个目录对"只能靠结构容忍：路径必须是数据（RuntimePaths），不得是代码常量。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from skillos.core.algorithm.adapters import (
    agents as agents_mod,
)
from skillos.core.algorithm.adapters import (
    claude as claude_mod,
)
from skillos.core.algorithm.adapters import (
    codex as codex_mod,
)
from skillos.core.algorithm.adapters import (
    workbuddy as workbuddy_mod,
)
from skillos.core.algorithm.adapters.agents import AgentsAdapter
from skillos.core.algorithm.adapters.base import PathSpec, RuntimeAdapter, RuntimePaths
from skillos.core.algorithm.adapters.claude import ClaudeAdapter
from skillos.core.algorithm.adapters.codex import CodexAdapter
from skillos.core.algorithm.adapters.workbuddy import WorkBuddyAdapter
from skillos.core.model import FileEntry, SkillMeta, SkillSnapshot

ALL_ADAPTERS = [ClaudeAdapter, AgentsAdapter, WorkBuddyAdapter, CodexAdapter]

_CANONICAL_SKILL_MD = (
    b"---\n"
    b"name: code-review\n"
    b"description: \xe5\xae\xa1\xe6\x9f\xa5\xe4\xbb\xa3\xe7\xa0\x81\n"
    b"version: 1.2.0\n"
    b"agent_created: true\n"
    b"origin: ECC\n"
    b"allowed-tools:\n"
    b"  - Read\n"
    b"  - Bash\n"
    b"---\n"
    b"\xe6\xad\xa3\xe6\x96\x87\xe5\x86\x85\xe5\xae\xb9\n"
)


def _snapshot() -> SkillSnapshot:
    meta = SkillMeta(
        name="code-review",
        description="审查代码",
        version="1.2.0",
        agent_created=True,
        origin="ECC",
        allowed_tools=["Read", "Bash"],
        extra={"custom-field": "kept"},
    )
    files = (
        FileEntry(path="SKILL.md", content=_CANONICAL_SKILL_MD),
        FileEntry(path="scripts/run.py", content=b"print(1)\n"),
        FileEntry(path="references/a.md", content=b"ref\n"),
        FileEntry(path="assets/logo.png", content=b"\x89PNG\r\n", is_text=False),
        FileEntry(path="big.bin", content=b"", in_git=False),
    )
    return SkillSnapshot(skill_id="s1", slug="code-review", meta=meta, files=files)


class TestRenderLayoutPurity:
    """T6/T7：render_layout 必须是纯函数 —— base 可精确重建的前提（NFR-3.5）。

    坑（变异验证）：若实现掺入时间戳 / 随机值 / 本机路径，历史 base 会静默算错，
    本测试必须变红。
    """

    @pytest.mark.parametrize("cls", ALL_ADAPTERS)
    def test_deterministic_bytes(self, cls: type[RuntimeAdapter]) -> None:
        a = cls().render_layout(_snapshot())
        b = cls().render_layout(_snapshot())
        assert a == b

    @pytest.mark.parametrize("cls", ALL_ADAPTERS)
    def test_only_managed_files(self, cls: type[RuntimeAdapter]) -> None:
        """未纳管资产（in_git=False）不分发（FR-1.0.20）。"""
        out = cls().render_layout(_snapshot())
        assert "big.bin" not in out
        assert "SKILL.md" in out


class TestRenderLayoutContent:
    def test_claude_skill_md_rebuild(self) -> None:
        """SKILL.md 由 frontmatter_out 重建：drop 的字段消失、正文保留。"""
        out = ClaudeAdapter().render_layout(_snapshot())
        rendered = out["SKILL.md"].decode("utf-8")
        assert rendered.startswith("---\n")
        assert "version:" not in rendered          # claude drop
        assert "origin:" not in rendered
        assert "agent_created:" not in rendered
        assert "custom-field: kept" in rendered    # extra 透传
        assert "allowed-tools:" in rendered
        assert rendered.endswith("正文内容\n")      # 正文保留
        # frontmatter 与 frontmatter_out 一致（同一序列化路径）
        assert "name: code-review" in rendered
        assert "description: 审查代码" in rendered

    def test_claude_passes_category_files(self) -> None:
        out = ClaudeAdapter().render_layout(_snapshot())
        assert out["scripts/run.py"] == b"print(1)\n"
        assert out["references/a.md"] == b"ref\n"
        assert out["assets/logo.png"] == b"\x89PNG\r\n"  # 字节级透传，不归一化

    def test_agents_sidecar_rendered(self) -> None:
        out = AgentsAdapter().render_layout(_snapshot())
        assert "agents/openai.yaml" in out
        assert b"display_name" in out["agents/openai.yaml"]
        rendered = out["SKILL.md"].decode("utf-8")
        assert "version: 1.2.0" in rendered          # agents keep

    def test_codex_drops_version(self) -> None:
        rendered = CodexAdapter().render_layout(_snapshot())["SKILL.md"].decode("utf-8")
        assert "version:" not in rendered
        assert "agent_created:" not in rendered


class TestPathSpec:
    def test_project_template_must_be_relative(self) -> None:
        with pytest.raises(ValueError):
            PathSpec(key="builtin:x", scope="project", template="/abs/path", source="builtin")

    def test_project_template_no_dotdot(self) -> None:
        with pytest.raises(ValueError):
            PathSpec(key="builtin:x", scope="project", template="../escape", source="builtin")

    def test_user_template_allows_absolute(self) -> None:
        PathSpec(key="builtin:x", scope="user", template="{home}/.claude/skills", source="builtin")


class TestResolveRoot:
    """部署根 ≠ 发现根（FR-1.0.25）：resolve_root 返回部署根。"""

    def test_user_scope(self, tmp_path: Path) -> None:
        root = ClaudeAdapter().resolve_root("user", home=tmp_path)
        assert root == tmp_path / ".claude" / "skills"

    def test_project_scope(self, tmp_path: Path) -> None:
        root = ClaudeAdapter().resolve_root("project", home=tmp_path, project_dir=tmp_path / "proj")
        assert root == tmp_path / "proj" / ".claude" / "skills"

    def test_fallback_used_when_deploy_missing(self, tmp_path: Path) -> None:
        (tmp_path / ".claude-fallback").mkdir()
        adapter = ClaudeAdapter()
        # 注入 fallback 数据（路径即数据：测试通过覆盖数据改变行为，不改代码）
        adapter.paths["user"] = RuntimePaths(
            deploy_dir=PathSpec("builtin:claude", "user", "{home}/.claude/skills", "builtin"),
            scan_only_dirs=[],
            fallback_dirs=[
                PathSpec("builtin:claude", "user", "{home}/.claude-fallback", "builtin")
            ],
            shared_dirs=[],
            recursive_scan=True,
            verified_at=None,
            evidence="local-observed",
        )
        assert adapter.resolve_root("user", home=tmp_path) == tmp_path / ".claude-fallback"

    def test_returns_deploy_when_none_exists(self, tmp_path: Path) -> None:
        """deploy 与 fallback 都不存在 → 返回 deploy（首装），不回退硬编码默认。"""
        root = AgentsAdapter().resolve_root("user", home=tmp_path)
        assert root == tmp_path / ".agents" / "skills"

    def test_scan_roots_discovery_set(self, tmp_path: Path) -> None:
        """发现集合 ⊇ 部署根（deploy + scan_only + fallback）。"""
        adapter = AgentsAdapter()
        roots = adapter.scan_roots("user", home=tmp_path)
        assert (tmp_path / ".agents" / "skills") in roots


class TestPathsAsData:
    """T12 / FR-1.0.25：adapters/ 中不得出现字面量路径 —— 新增 Runtime 只加数据。

    坑：上游手工维护路径常量，几处改一处漏；此处为变异验证 ——
    谁在 adapter 源码里写回 Path.home() / 字面量目录，本测试变红。
    """

    @pytest.mark.parametrize(
        ("module_name", "module"),
        [
            ("claude", claude_mod),
            ("agents", agents_mod),
            ("workbuddy", workbuddy_mod),
            ("codex", codex_mod),
        ],
    )
    def test_no_literal_paths_in_adapter_source(self, module_name: str, module) -> None:
        src = inspect.getsource(module)
        assert "Path.home" not in src
        for lit in ('".claude"', '".agents"', '".workbuddy"', '".codex"', "'/.claude'"):
            assert lit not in src

    def test_every_adapter_has_runtime_paths(self) -> None:
        for cls in ALL_ADAPTERS:
            adapter = cls()
            assert isinstance(adapter.paths, dict)
            assert "user" in adapter.paths and "project" in adapter.paths
            for paths in adapter.paths.values():
                assert isinstance(paths, RuntimePaths)
                assert paths.deploy_dir.source == "builtin"
                assert paths.deploy_dir.key.startswith("builtin:")
                assert paths.evidence in ("official-doc", "upstream-source", "local-observed")
