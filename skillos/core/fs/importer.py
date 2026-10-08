"""纳管器（04 §2.2.5，FR-1.0.16 / FR-1.1.3）。

命名说明：文档中称 import.py，但 import 是 Python 关键字、无法作为模块名导入，
故本文件命名为 importer.py（模块内容与文档 §2.2.5 一一对应）。

核心前提：同一 slug 出现在多个 Runtime 是常态，不是异常（本机实测 brainstorming
在 .claude 与 .agents 下字节一致）。把多副本当错误处理，62 个 skill 首次纳管当场失败一半。

纳管决策表（04 §2.2.5）：
- 同 slug 多副本内容一致（pack_hash 相同）→ 合并一条 Skill + 多 binding，不报错不询问
- 同 slug 多副本内容不一致 → NAME_CONFLICT，列差异摘要，用户选主副本
- slug 仅大小写不同（Foo/foo）→ NAME_COLLISION，拒绝纳管（NFR-7.4）
- 目录无 SKILL.md → 跳过记 warning（scan 层已天然过滤；meta 解析失败记 warning）
- 同 slug 已纳管 → 幂等更新 binding

身份识别走 identity 分层匹配（core/algorithm/identity.py），不以 slug 字符串
相等为唯一依据（07 N8：Code Review 与 code-review 并存时按 slug 合并会覆盖错记录）。

plan_import 是纯决策函数（不碰 IO / DB）；落库与 canonical 复制由 services 层
按 plan 执行（baseline_source=imported，不自动 push —— 字段归一化未经用户确认
不得写回用户文件）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from skillos.core.algorithm.identity import IdentityCandidate, resolve_identity
from skillos.core.fs.safe_path import path_key
from skillos.core.model import ScannedSkill


class ImportError_(Exception):
    """纳管失败。code 取 NAME_CONFLICT / NAME_COLLISION（04 §6.1）。"""

    def __init__(self, code: str, message: str, candidates: list[ScannedSkill] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.candidates = candidates


@dataclass(frozen=True)
class ImportResult:
    """services 层执行纳管后的结果（P1-engine-close 阶段接线时使用）。"""

    skill_id: str
    bindings_created: int
    merged_duplicates: list[str]  # 被合并的同内容副本 runtime_key
    warnings: list[str]


@dataclass(frozen=True)
class PlannedSkill:
    """一个待落库的 Skill：主副本 + 全部副本 binding。skill_id=None 表示新建。"""

    slug: str
    skill_id: str | None
    primary: ScannedSkill
    bindings: list[ScannedSkill] = field(default_factory=list)


@dataclass(frozen=True)
class ImportConflict:
    """NAME_CONFLICT：同 slug 多副本内容不一致，等用户选主副本。"""

    code: str = "NAME_CONFLICT"
    slug: str = ""
    candidates: list[ScannedSkill] = field(default_factory=list)


@dataclass(frozen=True)
class ImportCollision:
    """NAME_COLLISION（NFR-7.4）：归一化同键、写法不同且内容不一致，拒绝纳管。"""

    code: str = "NAME_COLLISION"
    slug: str = ""
    candidates: list[ScannedSkill] = field(default_factory=list)


@dataclass(frozen=True)
class ImportPlan:
    skills: list[PlannedSkill] = field(default_factory=list)
    conflicts: list[ImportConflict] = field(default_factory=list)
    collisions: list[ImportCollision] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# 主副本建议顺序（用户可在 import 时覆盖）
PRIMARY_ORDER: tuple[str, ...] = ("claude", "agents", "workbuddy", "codex")


def _primary_rank(s: ScannedSkill) -> tuple[int, str]:
    try:
        return PRIMARY_ORDER.index(s.runtime_key), s.rel_path
    except ValueError:
        return len(PRIMARY_ORDER), s.rel_path


def _slug_of(s: ScannedSkill) -> str:
    return s.rel_path.rsplit("/", 1)[-1]


def plan_import(
    scanned: list[ScannedSkill],
    existing: list[IdentityCandidate],
) -> ImportPlan:
    """按纳管决策表对扫描结果分组决策（纯函数）。

    分组键 = path_key(目录名)（分段 NFC + casefold）—— 只比大小写会漏掉
    中文名的 NFC/NFD 差异；真实 slug 保留原写法（path_key 不得替换真实路径）。
    """
    plan = ImportPlan()
    for s in scanned:
        if s.meta is None:
            plan.warnings.append(f"frontmatter 解析失败，跳过元数据: {s.rel_path}")

    groups: dict[str, list[ScannedSkill]] = {}
    original_names: dict[str, set[str]] = {}
    for s in scanned:
        key = path_key(_slug_of(s))
        groups.setdefault(key, []).append(s)
        original_names.setdefault(key, set()).add(_slug_of(s))

    for key, members in sorted(groups.items()):
        hashes = {m.pack_hash for m in members}
        names = original_names[key]
        if len(hashes) == 1:
            # 内容一致 → 合并一条 Skill + 多 binding
            primary = min(members, key=_primary_rank)
            slug = _slug_of(primary)
            # 身份分层匹配既有记录（幂等绑定）
            resolution = resolve_identity(
                meta=primary.meta or members[0].meta,
                content_hash=primary.pack_hash,
                source_ref=None,
                existing=existing,
                dir_name=slug,
            )
            skill_id: str | None = None
            if resolution.status == "matched" and resolution.identity is not None:
                skill_id = resolution.identity.skill_id
            elif resolution.status == "ambiguous":
                # identity 多候选：不认，按 NAME_CONFLICT 上报（禁止自行挑一个）
                plan.conflicts.append(
                    ImportConflict(slug=slug, candidates=list(members))
                )
                continue
            plan.skills.append(
                PlannedSkill(slug=slug, skill_id=skill_id, primary=primary, bindings=list(members))
            )
            continue
        # 内容不一致：目录名写法不同 → NAME_COLLISION（拒绝）；完全同名 → NAME_CONFLICT
        if len(names) > 1:
            plan.collisions.append(
                ImportCollision(slug=sorted(names)[0], candidates=list(members))
            )
        else:
            plan.conflicts.append(
                ImportConflict(slug=next(iter(names)), candidates=list(members))
            )
    return plan
