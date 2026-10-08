"""OpenAI Agent Skills 适配器（~/.agents）。

字段策略：version / agent_created 均 keep。
额外产出 sidecar `agents/openai.yaml`（本机 30 个 Skill 带此文件，04 §2.1.2）。
路径全部在 paths 数据里（FR-1.0.25）。
"""

from __future__ import annotations

from datetime import date

from skillos.core.algorithm.adapters.base import PathSpec, RuntimeAdapter, RuntimePaths
from skillos.core.model import SkillMeta

_VERIFIED = date(2026, 10, 6)


def _paths() -> dict[str, RuntimePaths]:
    # 本机实测（2026-10-07）：skill 在 {home}/.agents/skills 子目录下，不是 .agents 根
    return {
        "user": RuntimePaths(
            deploy_dir=PathSpec("builtin:agents", "user", "{home}/.agents/skills", "builtin"),
            verified_at=_VERIFIED,
        ),
        "project": RuntimePaths(
            deploy_dir=PathSpec("builtin:agents", "project", "{project}/.agents/skills", "builtin"),
            verified_at=_VERIFIED,
        ),
    }


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
    paths = _paths()

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
