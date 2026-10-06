"""扫描器（04 §2.2.1，FR-1.0.1 / FR-1.0.3）。

包边界规则：
1. 含 SKILL.md 的目录即包根。
2. 包根下 subskills/<name>/SKILL.md 识别为子技能（is_nested=True），计入清单。
3. 跳过 .runtime/（商城安装产物）、node_modules/、.git/。
4. 符号链接不跟随（防环），记录为警告。

root 由调用方显式传入（T1）。
"""

from __future__ import annotations

from pathlib import Path

from skillos.core.model import RootSpec, ScannedSkill

DEFAULT_IGNORE = frozenset({".runtime", "node_modules", ".git"})


def scan_roots(roots: list[RootSpec], ignore: list[str] | None = None) -> list[ScannedSkill]:
    """扫描多个 Runtime 根，返回全部识别到的 Skill 包。

    单个目录解析失败降级继续（记 warning），不中断全量扫描（04 §6.1）。
    """
    raise NotImplementedError("P1 实现：目录树遍历 + SKILL.md 包边界识别 + pack_hash 计算")


def scan_root(root: Path, spec: RootSpec, ignore: frozenset[str]) -> list[ScannedSkill]:
    """单个根目录扫描（scan_roots 的实现单元）。"""
    raise NotImplementedError("P1 实现")
