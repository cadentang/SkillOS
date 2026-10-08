"""WorkBuddy 适配器（~/.workbuddy）。

字段策略：version / agent_created 均 keep（04 §2.1.2 实测表）。
路径全部在 paths 数据里（FR-1.0.25）。
"""

from __future__ import annotations

from datetime import date

from skillos.core.algorithm.adapters.base import PathSpec, RuntimeAdapter, RuntimePaths
from skillos.core.model import SkillMeta

_VERIFIED = date(2026, 10, 6)


def _paths() -> dict[str, RuntimePaths]:
    # 本机实测（2026-10-07）：.workbuddy 根是应用数据目录（blobs/plugins/audit-log...），
    # 真正的 skill 在 {home}/.workbuddy/skills 下 —— 根太宽会把 1500+ 个插件目录收编成假 Skill
    return {
        "user": RuntimePaths(
            deploy_dir=PathSpec("builtin:workbuddy", "user", "{home}/.workbuddy/skills", "builtin"),
            verified_at=_VERIFIED,
        ),
        "project": RuntimePaths(
            deploy_dir=PathSpec(
                "builtin:workbuddy", "project", "{project}/.workbuddy/skills", "builtin"
            ),
            verified_at=_VERIFIED,
        ),
    }


class WorkBuddyAdapter(RuntimeAdapter):
    key = "workbuddy"
    version = "1.0.0"  # T7
    supports = frozenset({"scripts", "references", "assets", "subskills"})
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
