"""领域数据类（02 §2 core/model.py）。

纯数据定义，无 IO、无第三方运行时依赖（除标准库）。
漂移判定算法见 core/algorithm/drift.py，扫描器见 core/fs/scan.py。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 通用


@dataclass(frozen=True)
class Commit:
    """git 提交摘要（GitRepo.log / version_log 的返回单元）。"""

    sha: str
    message: str
    author: str
    at: str  # ISO8601


@dataclass(frozen=True)
class RootSpec:
    """扫描根（04 §2.2.1）：一个 Runtime 目录 + 其 scope。"""

    runtime_key: str
    root: Path
    scope: str  # "user" | "project"


# ---------------------------------------------------------------- Skill 元数据


@dataclass(frozen=True)
class FileEntry:
    """文件清单单元：canonical 快照与磁盘扫描共用。"""

    path: str  # POSIX 分隔符（05 P2-3）
    content: bytes
    is_text: bool = True
    in_git: bool = True  # T2：False 表示超阈值未纳管资产，不参与漂移哈希


@dataclass(frozen=True)
class SkillMeta:
    """SKILL.md frontmatter 解析结果（04 §2.1.1）。

    未识别字段进 extra 并在分发时透传（OUT-9：不做语义转译）。
    """

    name: str
    description: str
    version: str | None = None
    agent_created: bool | None = None
    origin: str | None = None
    allowed_tools: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillFile:
    """解析后的 Skill 文件（含 frontmatter 规范化结果）。"""

    path: str
    meta: SkillMeta | None
    is_text: bool = True


@dataclass(frozen=True)
class SkillSnapshot:
    """某一 commit 的完整 canonical 快照 —— base 重建的唯一输入（NFR-3.5）。"""

    skill_id: str
    slug: str
    meta: SkillMeta
    files: tuple[FileEntry, ...]


# ---------------------------------------------------------------- 漂移


class DriftState(StrEnum):
    """漂移 7 态（T8）。UNMANAGED 吸并原 EXTRA。"""

    SYNCED = "synced"
    MISSING = "missing"
    OUTDATED = "outdated"
    MODIFIED = "modified"
    CONFLICT = "conflict"
    UNMANAGED = "unmanaged"
    ORPHANED = "orphaned"


@dataclass(frozen=True)
class DriftReport:
    """单 Skill × 单 Runtime 的漂移判定结果（04 §2.1.3）。

    adapter_outdated 与 has_untracked_assets 是独立维度，不进入状态枚举（FR-1.0.19 / FR-1.0.20）。
    """

    skill_id: str
    runtime_key: str
    state: DriftState
    base_commit: str | None           # runtime_bindings.installed_commit
    base_adapter_version: str | None  # runtime_bindings.adapter_version
    base_hash: str | None             # runtime_bindings.installed_hash（磁盘基线）
    local_hash: str | None            # 磁盘现状
    remote_hash: str | None           # 仓库经 adapter 渲染后应得
    adapter_outdated: bool = False    # 独立维度：UI 显示「模板已升级」徽标
    has_untracked_assets: bool = False  # 独立维度：UI 显示 SYNCED* + 徽标
    changed_files: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 扫描


@dataclass(frozen=True)
class ScannedSkill:
    """扫描器输出（04 §2.2.1）。"""

    root: Path
    rel_path: str
    runtime_key: str
    scope: str  # "user" | "project"
    meta: SkillMeta | None
    pack_hash: str
    is_nested: bool  # 位于 subskills/ 下
