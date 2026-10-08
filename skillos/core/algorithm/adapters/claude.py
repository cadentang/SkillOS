"""Claude Code 适配器（~/.claude/skills）。

字段策略（04 §2.1.2 实测表）：version / agent_created / origin 一律 drop。
路径全部在 paths 数据里（FR-1.0.25），本文件不出现字面量路径与渲染代码。
"""

from __future__ import annotations

from datetime import date

from skillos.core.algorithm.adapters.base import PathSpec, RuntimeAdapter, RuntimePaths
from skillos.core.model import SkillMeta

_VERIFIED = date(2026, 10, 6)  # 本机实测确认（BASELINE-1.0 冻结时的口径）


def _paths(project_template: str, user_template: str) -> dict[str, RuntimePaths]:
    return {
        "user": RuntimePaths(
            deploy_dir=PathSpec("builtin:claude", "user", user_template, "builtin"),
            verified_at=_VERIFIED,
        ),
        "project": RuntimePaths(
            deploy_dir=PathSpec("builtin:claude", "project", project_template, "builtin"),
            verified_at=_VERIFIED,
        ),
    }


class ClaudeAdapter(RuntimeAdapter):
    key = "claude"
    version = "1.0.0"  # T7：改字段策略 / sidecar / 布局必须 bump
    supports = frozenset({"scripts", "references", "assets", "subskills"})
    field_policy = {
        "name": "keep",
        "description": "keep",
        "allowed-tools": "keep",
        "version": "drop",
        "agent_created": "drop",
        "origin": "drop",
    }
    paths = _paths("{project}/.claude/skills", "{home}/.claude/skills")

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
