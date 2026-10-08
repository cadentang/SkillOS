"""Codex 适配器（~/.codex）。

字段策略：version / agent_created / origin 一律 drop（04 §2.1.2 实测表）。
路径全部在 paths 数据里（FR-1.0.25）。

坑（07 D1）：上游 CHANGELOG 对 Codex 的 skill 目录给出过两次相反结论
（~/.agents/skills vs ~/.codex/skills）—— "哪个对"不靠查证永久消除，
靠 RuntimePaths 结构容忍：用户可用 custom: 覆盖，verified_at 标注实测口径。
"""

from __future__ import annotations

from datetime import date

from skillos.core.algorithm.adapters.base import PathSpec, RuntimeAdapter, RuntimePaths
from skillos.core.model import SkillMeta

_VERIFIED = date(2026, 10, 6)


def _paths() -> dict[str, RuntimePaths]:
    # 坑（07 D1 / 本机实测 2026-10-07）：上游对 Codex 目录给过两次相反结论；
    # 本机 ~/.codex/skills 实测存在 5 个 skill，取此为 deploy 根。
    # ~/.codex/vendor_imports 下唯一另一处带 SKILL.md 的目录是外部 clone 的上游仓库
    # （含 .git），不是安装态 —— 不收编进发现根（FR-1.0.25：未列出的一律不扫）。
    return {
        "user": RuntimePaths(
            deploy_dir=PathSpec("builtin:codex", "user", "{home}/.codex/skills", "builtin"),
            verified_at=date(2026, 10, 7),
        ),
        "project": RuntimePaths(
            deploy_dir=PathSpec("builtin:codex", "project", "{project}/.codex/skills", "builtin"),
            verified_at=date(2026, 10, 7),
        ),
    }


class CodexAdapter(RuntimeAdapter):
    key = "codex"
    version = "1.0.0"  # T7
    supports = frozenset({"scripts", "references", "assets", "subskills"})
    field_policy = {
        "name": "keep",
        "description": "keep",
        "allowed-tools": "keep",
        "version": "drop",
        "agent_created": "drop",
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
