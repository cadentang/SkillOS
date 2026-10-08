"""稳定身份与分层匹配（04 §2.1.5 / FR-1.0.21）。

问题：同名不同内容、同内容改过名、同一 Skill 散在四个 Runtime —— 必须先回答
"这是哪一个"，才谈得上合并、漂移、溯源。

分层匹配（顺序即优先级，任一层多候选即不认）：
  1. source_ref 精确相等   → 唯一命中即确定（certain）
  2. 唯一内容哈希相等       → 仅当只有一个候选才确定（probable）
  3. dir/name 归一化相等   → 同上（path_key：分段 NFC + casefold）

为什么禁止按相似度取最高分（07 N8，变异验证背景）：并列时的"任意选择"会把
副本绑到错误的 Skill 上，「导回仓库」会写入匹配到的那一条 —— 结果是覆盖掉
另一个 Skill（上游 1.40.0 真实事故：Code Review 与 code-review 并存）。
宁缺勿滥：多一个待人工确认的冲突，好过多一条被静默覆盖的记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from skillos.core.fs.safe_path import path_key
from skillos.core.model import SkillMeta

_STATUS_MATCHED = "matched"
_STATUS_NEW = "new"
_STATUS_AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class IdentityCandidate:
    """库内既有 Skill 的身份锚点（由 store 层取出后传入，本模块不碰 IO）。"""

    skill_id: str
    source_ref: str | None = None
    content_hash: str | None = None
    dir_name: str | None = None


@dataclass(frozen=True)
class SkillIdentity:
    """匹配结论。confidence: certain（source_ref 唯一命中）| probable（第 2/3 层唯一命中）。"""

    skill_id: str
    confidence: str
    matched_by: str  # "source_ref" | "content_hash" | "dir_name"


@dataclass(frozen=True)
class IdentityResolution:
    """resolve_identity 的完整结论。

    - matched:   唯一确定，identity 可用
    - new:       无任何层命中 —— 全新 Skill，调用方新建记录
    - ambiguous: 某层出现多候选 —— 调用方必须报冲突，禁止自行挑一个（07 N8）
    """

    status: str
    identity: SkillIdentity | None = None
    candidates: list[IdentityCandidate] = field(default_factory=list)
    ambiguous_layer: str | None = None


def resolve_identity(
    meta: SkillMeta,
    content_hash: str | None,
    source_ref: str | None,
    existing: list[IdentityCandidate],
    dir_name: str | None = None,
) -> IdentityResolution:
    """分层匹配。返回 IdentityResolution；ambiguous 时调用方必须报冲突。"""
    # 第 1 层：source_ref 精确相等
    if source_ref is not None:
        hits = [c for c in existing if c.source_ref == source_ref]
        if len(hits) > 1:
            return IdentityResolution(_STATUS_AMBIGUOUS, None, hits, "source_ref")
        if len(hits) == 1:
            return IdentityResolution(
                _STATUS_MATCHED,
                SkillIdentity(hits[0].skill_id, "certain", "source_ref"),
            )

    # 第 2 层：唯一内容哈希相等（仅当只有一个候选才确定）
    if content_hash is not None:
        hits = [c for c in existing if c.content_hash == content_hash]
        if len(hits) > 1:
            return IdentityResolution(_STATUS_AMBIGUOUS, None, hits, "content_hash")
        if len(hits) == 1:
            return IdentityResolution(
                _STATUS_MATCHED,
                SkillIdentity(hits[0].skill_id, "probable", "content_hash"),
            )

    # 第 3 层：dir/name 归一化相等（path_key：分段 NFC + casefold）
    if dir_name is not None:
        key = path_key(dir_name)
        hits = [c for c in existing if c.dir_name is not None and path_key(c.dir_name) == key]
        if len(hits) > 1:
            return IdentityResolution(_STATUS_AMBIGUOUS, None, hits, "dir_name")
        if len(hits) == 1:
            return IdentityResolution(
                _STATUS_MATCHED,
                SkillIdentity(hits[0].skill_id, "probable", "dir_name"),
            )

    return IdentityResolution(_STATUS_NEW)
