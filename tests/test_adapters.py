"""adapter 单测：字段策略（04 §2.1.2 实测表）+ sidecar + 纯函数约束占位。"""

from __future__ import annotations

import pytest

from skillos.core.algorithm.adapters.agents import AgentsAdapter
from skillos.core.algorithm.adapters.base import RuntimeAdapter
from skillos.core.algorithm.adapters.claude import ClaudeAdapter
from skillos.core.algorithm.adapters.codex import CodexAdapter
from skillos.core.algorithm.adapters.workbuddy import WorkBuddyAdapter
from skillos.core.model import SkillMeta


def _meta() -> SkillMeta:
    return SkillMeta(
        name="code-review",
        description="审查代码",
        version="1.2.0",
        agent_created=True,
        origin="ECC",
        allowed_tools=["Read", "Bash"],
        extra={"custom-field": "kept"},
    )


ALL_ADAPTERS = [
    ClaudeAdapter,
    AgentsAdapter,
    WorkBuddyAdapter,
    CodexAdapter,
]


class TestFieldPolicy:
    """canonical 字段 keep/drop 必须与 04 §2.1.2 实测表逐格一致。"""

    @pytest.mark.parametrize("cls", ALL_ADAPTERS)
    def test_name_description_always_keep(self, cls: type[RuntimeAdapter]) -> None:
        out = cls().frontmatter_out(_meta())
        assert out["name"] == "code-review"
        assert out["description"] == "审查代码"

    @pytest.mark.parametrize("cls", [ClaudeAdapter, AgentsAdapter, WorkBuddyAdapter, CodexAdapter])
    def test_allowed_tools_keep(self, cls: type[RuntimeAdapter]) -> None:
        assert cls().frontmatter_out(_meta())["allowed-tools"] == ["Read", "Bash"]

    @pytest.mark.parametrize("cls", [ClaudeAdapter, CodexAdapter])
    def test_version_dropped(self, cls: type[RuntimeAdapter]) -> None:
        assert "version" not in cls().frontmatter_out(_meta())
        assert "agent_created" not in cls().frontmatter_out(_meta())
        assert "origin" not in cls().frontmatter_out(_meta())

    @pytest.mark.parametrize("cls", [AgentsAdapter, WorkBuddyAdapter])
    def test_version_kept(self, cls: type[RuntimeAdapter]) -> None:
        out = cls().frontmatter_out(_meta())
        assert out["version"] == "1.2.0"
        assert out["agent_created"] is True
        assert "origin" not in out  # origin 四端全 drop

    @pytest.mark.parametrize("cls", ALL_ADAPTERS)
    def test_extra_passthrough(self, cls: type[RuntimeAdapter]) -> None:
        """OUT-9：未识别字段原样透传，不做语义转译。"""
        assert cls().frontmatter_out(_meta())["custom-field"] == "kept"

    @pytest.mark.parametrize("cls", ALL_ADAPTERS)
    def test_none_fields_not_emitted(self, cls: type[RuntimeAdapter]) -> None:
        meta = SkillMeta(name="x", description="y")
        out = cls().frontmatter_out(meta)
        assert "version" not in out and "origin" not in out


class TestSidecar:
    def test_agents_openai_yaml(self) -> None:
        spec = AgentsAdapter().sidecar_specs(_meta())
        assert "agents/openai.yaml" in spec
        content = spec["agents/openai.yaml"]
        assert 'display_name: "code-review"' in content
        assert "allow_implicit_invocation: true" in content

    def test_others_no_sidecar(self) -> None:
        for cls in (ClaudeAdapter, WorkBuddyAdapter, CodexAdapter):
            assert cls().sidecar_specs(_meta()) == {}


class TestVersion:
    """T7：字段策略 / sidecar / 布局变更必须 bump version —— 版本常量必须存在。"""

    @pytest.mark.parametrize("cls", ALL_ADAPTERS)
    def test_version_declared(self, cls: type[RuntimeAdapter]) -> None:
        assert cls.version and cls.version != "0.0.0"

    @pytest.mark.parametrize("cls", ALL_ADAPTERS)
    def test_keys_distinct(self, cls: type[RuntimeAdapter]) -> None:
        assert cls.key in {"claude", "agents", "workbuddy", "codex"}


# T6/T7：render_layout 纯函数单测（同输入两次调用逐字节相同）在 P1 实现 render 后补齐，
# 见 04 §8.2 风险 9 —— 该测试是 base 可信的前提，必须纳入 P1 验收。
