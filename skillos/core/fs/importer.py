"""纳管器（04 §2.2.5，FR-1.0.16 / FR-1.1.3）。

命名说明：文档中称 import.py，但 import 是 Python 关键字、无法作为模块名导入，
故本文件命名为 importer.py（模块内容与文档 §2.2.5 一一对应）。

核心前提：同一 slug 出现在多个 Runtime 是常态，不是异常（本机实测 brainstorming
在 .claude 与 .agents 下字节一致）。把多副本当错误处理，62 个 skill 首次纳管当场失败一半。

纳管决策表（04 §2.2.5）：
- 同 slug 多副本内容一致（pack_hash 相同）→ 合并一条 Skill + 多 binding，不报错不询问
- 同 slug 多副本内容不一致 → NAME_CONFLICT，列差异摘要，用户选主副本
- slug 仅大小写不同 → NAME_COLLISION，拒绝纳管（NFR-7.4）
- 目录无 SKILL.md → 跳过记 warning
- 同 slug 已纳管 → 幂等更新 binding

基线来源 baseline_source：
- distributed（push 写入）→ 可重建 base，完整三方比对
- imported（纳管用户目录）→ 磁盘是用户写的，无 base，只做两方比对（不进 CONFLICT）
- 首次 push 成功后自动转 distributed

不自动 push：纳管会做字段归一化（如 origin 被 drop），自动写回等于未经确认改用户文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from skillos.core.model import RootSpec, ScannedSkill


class ImportError_(Exception):
    """纳管失败。code 取 NAME_CONFLICT / NAME_COLLISION（04 §6.1）。"""

    def __init__(self, code: str, message: str, candidates: list[ScannedSkill] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.candidates = candidates


@dataclass(frozen=True)
class ImportResult:
    skill_id: str
    bindings_created: int
    merged_duplicates: list[str]  # 被合并的同内容副本 runtime_key
    warnings: list[str]


# 主副本建议顺序（用户可在 import 时覆盖）
PRIMARY_ORDER: tuple[str, ...] = ("claude", "agents", "workbuddy", "codex")


def import_scanned(
    root: Path,
    scanned: list[ScannedSkill],
    roots: list[RootSpec],
) -> ImportResult:
    """将扫描结果纳管入库。实现待 P1。"""
    raise NotImplementedError("P1 实现：决策表 + 主副本选择 + binding 基线记录")
