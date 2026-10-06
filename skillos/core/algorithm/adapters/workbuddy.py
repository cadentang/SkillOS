"""WorkBuddy 适配器（~/.workbuddy）。

字段策略：version / agent_created 均 keep（04 §2.1.2 实测表）。
"""

from __future__ import annotations

from pathlib import Path

from skillos.core.algorithm.adapters.base import RuntimeAdapter
from skillos.core.model import SkillMeta, SkillSnapshot


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

    def resolve_root(self, scope: str, project_dir: Path | None) -> Path:
        if scope == "project":
            assert project_dir is not None, "project scope 需要 project_dir"
            return project_dir / ".workbuddy"
        return Path.home() / ".workbuddy"

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

    def render_layout(self, snapshot: SkillSnapshot) -> dict[str, bytes]:
        raise NotImplementedError("P1 实现：纯函数（T6/T7）")
