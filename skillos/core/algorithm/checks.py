"""质量检查规则集（04 §2.1.4，FR-1.0.11）。纯函数，无 IO。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CheckLevel(StrEnum):
    ERROR = "error"
    WARN = "warn"
    INFO = "info"


@dataclass(frozen=True)
class CheckResult:
    rule: str
    level: CheckLevel
    message: str


# 规则清单（rule ID 即白名单，新增规则必须同步 04 §2.1.4 表）
RULES: frozenset[str] = frozenset(
    {
        "fm.name.required",
        "fm.description.required",
        "fm.description.length",
        "fm.description.tokens",
        "fm.name.kebab",
        "fm.unknown",
        "struct.skill_md",
        "struct.ref.missing",
        "dep.missing",
        "dep.cycle",
    }
)


def check_skill(meta_ok: bool, description: str | None, unknown_fields: list[str]) -> list[CheckResult]:
    """占位实现：P1 按 04 §2.1.4 表逐条落地（含 struct / dep 规则需文件清单与依赖图）。"""
    raise NotImplementedError("P1 实现：按规则表逐条检查，纯函数")


def estimate_tokens(text: str) -> int:
    """粗略 token 估算（fm.description.tokens 用）：中文约 1 字 ≈ 1 token，英文约 4 字符 ≈ 1 token。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + other // 4
