"""Runtime 约定俗成探测（FR-1.0.32 / T24 / D8 / FR-2.4.10）。

白名单 = 数据（builtin_potential_runtimes.yaml），本模块**零字面量
路径**（`~/.cursor` 等），新增 / 删除探测项只动 yaml。命中判据仅 =
该路径下存在至少一个 SKILL.md（沿用 list_content_files 单一入口，T16）。

不做什么（D8 红线）：
- 不写磁盘
- 不写 binding
- 不进漂移矩阵列头（候选 ≠ 已纳管）
- 不自动 import SKILL.md
- 触发权完全在用户（Settings 页"加入"按钮）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from skillos.core.algorithm.hash import FileEntry, list_content_files

_WHITELIST_PATH = Path(__file__).parent / "builtin_potential_runtimes.yaml"


@dataclass(frozen=True)
class Candidate:
    """一个白名单命中（或未命中）的潜在 Runtime。"""

    key: str
    display_name: str
    root_path: Path
    path_exists: bool
    skill_count: int  # 含 SKILL.md 的子目录数（命中 = >= 1）
    verified_at: date | None
    evidence: str
    skipped: bool  # yaml skip=true → 不探测，仅占位

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.display_name,
            "path": str(self.root_path),
            "path_exists": self.path_exists,
            "skill_count": self.skill_count,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "evidence": self.evidence,
            "skipped": self.skipped,
        }


def _load_whitelist() -> list[dict[str, Any]]:
    """读白名单 yaml；IO 失败抛（Settings 页错误由 daemon task 透传）。"""
    with _WHITELIST_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return list(data) if data else []


def _parse_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError:
            return None
    return None


def _count_skill_dirs(root: Path) -> int:
    """命中判据（T16 单一入口）：root 下含 SKILL.md 的直接子目录数。

    沿用 list_content_files 单一入口，不另立枚举；list_content_files 在
    root 不存在时抛 FileNotFoundError，由调用方 try/except 转为 0 命中。
    """
    try:
        files: list[FileEntry] = list_content_files(root)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return 0
    # 含 SKILL.md 的直接子目录集合（命中 = 至少 1）
    parents: set[Path] = set()
    for f in files:
        if str(f.path).endswith("SKILL.md"):
            parents.add((root / str(f.path)).parent)
    return len(parents)


def discover_potential_runtimes(home: Path) -> list[Candidate]:
    """按白名单 + home 探测候选（FR-1.0.32 / T24）。

    返回顺序按 yaml 出现次序；每条都返回（含未命中：path_exists=False /
    skill_count=0），让前端"未纳管候选"段一次性展示。
    """
    items: list[Candidate] = []
    for entry in _load_whitelist():
        key = str(entry.get("key") or "").strip()
        if not key:
            continue
        skipped = bool(entry.get("skip"))
        relpath = str(entry.get("home_relpath") or "")
        try:
            root = Path(relpath.replace("{home}", str(home)))
        except (KeyError, ValueError):
            continue
        if skipped:
            items.append(Candidate(
                key=key,
                display_name=str(entry.get("display_name") or key),
                root_path=root,
                path_exists=False,
                skill_count=0,
                verified_at=_parse_date(entry.get("verified_at")),
                evidence=str(entry.get("evidence") or "local-observed"),
                skipped=True,
            ))
            continue
        exists = root.exists()
        count = _count_skill_dirs(root) if exists else 0
        items.append(Candidate(
            key=key,
            display_name=str(entry.get("display_name") or key),
            root_path=root,
            path_exists=exists,
            skill_count=count,
            verified_at=_parse_date(entry.get("verified_at")),
            evidence=str(entry.get("evidence") or "local-observed"),
            skipped=False,
        ))
    return items
