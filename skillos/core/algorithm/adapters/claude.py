"""Claude Code 适配器（~/.claude/skills）。

字段策略（04 §2.1.2 实测表）：version / agent_created / origin 一律 drop。
"""

from __future__ import annotations

from pathlib import Path

from skillos.core.algorithm.adapters.base import RuntimeAdapter
from skillos.core.model import SkillMeta, SkillSnapshot


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

    def resolve_root(self, scope: str, project_dir: Path | None) -> Path:
        # T1: 调用方注入基础目录；scope=project 时挂 .claude/skills
        if scope == "project":
            assert project_dir is not None, "project scope 需要 project_dir"
            return project_dir / ".claude" / "skills"
        return Path.home() / ".claude" / "skills"

    def frontmatter_in(self, raw: dict) -> SkillMeta:
        from skillos.core.algorithm.parse import parse_frontmatter  # noqa: F401（复用降级规则）

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
        raise NotImplementedError("P1 实现：渲染 SKILL.md + 纳管文件，纯函数（T6/T7）")
