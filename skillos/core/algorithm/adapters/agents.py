"""OpenAI Agent Skills 适配器（~/.agents）。

字段策略：version / agent_created 均 keep。
额外产出 sidecar `agents/openai.yaml`（本机 30 个 Skill 带此文件，04 §2.1.2）。
"""

from __future__ import annotations

from pathlib import Path

from skillos.core.algorithm.adapters.base import RuntimeAdapter
from skillos.core.model import SkillMeta, SkillSnapshot


class AgentsAdapter(RuntimeAdapter):
    key = "agents"
    version = "1.0.0"  # T7
    supports = frozenset({"scripts", "references", "assets", "subskills", "sidecar"})
    field_policy = {
        "name": "keep",
        "description": "keep",
        "allowed-tools": "keep",
        "version": "keep",
        "agent_created": "keep",
        "origin": "drop",
    }

    def resolve_root(self, scope: str, project_dir: Path | None) -> Path:
        if scope == "project":
            assert project_dir is not None, "project scope 需要 project_dir"
            return project_dir / ".agents"
        return Path.home() / ".agents"

    def frontmatter_in(self, raw: dict) -> SkillMeta:
        return SkillMeta(
            name=raw.get("name", ""),
            description=raw.get("description", ""),
            version=raw.get("version"),
            agent_created=raw.get("agent_created"),
            origin=raw.get("origin"),
            allowed_tools=raw.get("allowed-tools"),
            extra={k: v for k, v in raw.items() if k not in self.field_policy},
        )

    def sidecar_specs(self, meta: SkillMeta) -> dict[str, str]:
        # 纯函数：只依赖 meta，不含时间戳 / 路径
        return {
            "agents/openai.yaml": (
                "interface:\n"
                f'  display_name: "{meta.name}"\n'
                f'  short_description: "{meta.description[:80]}"\n'
                f'  default_prompt: "{meta.description}"\n'
                "policy:\n"
                "  allow_implicit_invocation: true\n"
            )
        }

    def render_layout(self, snapshot: SkillSnapshot) -> dict[str, bytes]:
        raise NotImplementedError("P1 实现：渲染 SKILL.md + openai.yaml sidecar，纯函数（T6/T7）")
