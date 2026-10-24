"""质量检查规则集（04 §2.1.4，FR-1.0.11 / NFR-10.5）。纯函数，无 IO。

门禁语义（"检查"与"门禁"的区别）：
- error 拦截：push 前置校验遇 error 必须中止，除非显式 --allow-gate-failure
  （该开关不得出现在 MCP 路径，FR-1.3.6）
- 判定只有一份实现（NFR-10.5）：CLI check / services.push 前置 / 云端 API
  全部调用 run_gate()。两处判定漂移会导致"CLI 通过而 push 拦截"。
- 结果落库（skill_checks）由 services 层承担，本模块只产出 findings。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from skillos.core.constants import (
    DESC_MAX_CHARS,
    DESC_MIN_CHARS,
    DESC_TOKEN_HINT,
    HEALTH_SCORE_FLOOR,
    HEALTH_SCORE_MAX,
    HEALTH_WEIGHTS,
    STALENESS_DEGRADED_DAYS,
)
from skillos.core.model import SkillSnapshot


class CheckLevel(StrEnum):
    ERROR = "error"
    WARN = "warn"
    INFO = "info"


@dataclass(frozen=True)
class RuleFinding:
    rule: str
    level: CheckLevel
    message: str


@dataclass(frozen=True)
class GateContext:
    """判定所需的外部事实（调用方取回后传入，本模块不碰 IO）。"""

    known_slugs: frozenset[str] = frozenset()      # 库内全部 slug（dep.missing）
    dependencies: tuple[str, ...] = ()               # 本 Skill 声明的依赖 slug
    dependency_graph: dict[str, list[str]] = field(default_factory=dict)  # dep.cycle


@dataclass(frozen=True)
class GateResult:
    findings: list[RuleFinding]
    has_error: bool  # 门禁判定只看这一位


class RuleId(StrEnum):
    """规则 ID 契约（E-03.2）。

    Web 下钻、MCP 错误信封（`GATE_FAILED` 明细）、CLI `--json` 都引用它 ——
    重命名 / 删除规则 = 破坏性变更，须在 `00-需求定稿.md` 立项。
    代码里**只准引用本枚举**，不写字面量（否则改名时漏改一处就断链）。
    """

    NAME_REQUIRED = "fm.name.required"
    DESCRIPTION_REQUIRED = "fm.description.required"
    DESCRIPTION_LENGTH = "fm.description.length"
    DESCRIPTION_TOKENS = "fm.description.tokens"
    NAME_KEBAB = "fm.name.kebab"
    UNKNOWN_FIELD = "fm.unknown"
    SKILL_MD_MISSING = "struct.skill_md"
    REF_MISSING = "struct.ref.missing"
    DEP_MISSING = "dep.missing"
    DEP_CYCLE = "dep.cycle"
    ASSET_UNTRACKED = "asset.untracked"  # 排期 P1 新增（FR-1.0.20）


# 规则清单（rule ID 即白名单，新增规则必须同步 04 §2.1.4 表）
RULES: frozenset[str] = frozenset(item.value for item in RuleId)

# description 长度区间与 token 提示阈值已归位 core/constants.py（E-03.3），此处只引用。

# 折旧三维（E-02）：健康度合成时按维度计 degraded 次数
DECAY_DIMENSIONS: tuple[str, str, str] = (
    "staleness",
    "dependency_broken",
    "upstream_divergence",
)
_DECAY_STATUSES: frozenset[str] = frozenset({"ok", "degraded", "unknown"})

_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# 正文中的相对路径引用：markdown 链接 / 图片
_REF = re.compile(r"(?:\]\(|src=\")(?!https?://|#|/)([^)\"\\]+?)(?:\)|\")")

_CANONICAL_FM_KEYS = frozenset(
    {"name", "description", "version", "agent_created", "origin", "allowed-tools"}
)


def is_kebab(value: str) -> bool:
    """kebab-case 校验：fm.name.kebab 与 CRUD slug 共用同一口径（NFR-10.5 同源思想）。"""
    return bool(_KEBAB.match(value))


def estimate_tokens(text: str) -> int:
    """粗略 token 估算（fm.description.tokens 用）。

    中文约 1 字 ≈ 1 token，英文约 4 字符 ≈ 1 token。
    """
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + other // 4


def _has_cycle(graph: dict[str, list[str]], start: str) -> bool:
    """DFS 检环（从 start 出发能回到 start 即有环）。"""
    seen: set[str] = set()
    stack = list(graph.get(start, []))
    while stack:
        node = stack.pop()
        if node == start:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, []))
    return False


def _body_of(files) -> str:
    skill_md = next((f for f in files if f.path == "SKILL.md"), None)
    if skill_md is None:
        return ""
    try:
        return skill_md.content.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _decay_status(decay: Mapping[str, object], dim: str) -> str:
    """取单个折旧维度的状态（ok / degraded / unknown）。

    兼容两代形态：E-02 的结构化结果（`{"staleness": "degraded", ...}`）与
    P3 的扁平结果（`{"staleness_days": 12.5, "dependency_broken": [...]}`）。
    判不出来一律 unknown —— 不猜（NFR-10.4）。
    """
    # 优先读显式状态键（依赖维度与旧字段同名，故状态用 `<dim>_status` 避免撞车）
    for key in (f"{dim}_status", dim):
        raw = decay.get(key)
        if isinstance(raw, str) and raw in _DECAY_STATUSES:
            return raw
    if dim == "staleness":
        days = decay.get("staleness_days")
        if not _is_number(days):
            return "unknown"
        return "degraded" if days >= STALENESS_DEGRADED_DAYS else "ok"  # type: ignore[operator]
    if dim == "dependency_broken":
        broken = decay.get(dim)
        if not isinstance(broken, (list, tuple)):
            return "unknown"   # 声明格式不可解析 / 字段缺失：不猜（E-02.4）
        return "degraded" if len(broken) > 0 else "ok"
    return "unknown"           # upstream_divergence：E-08 启用前恒 unknown（E-02.5）


def _decay_value(decay: Mapping[str, object]) -> Mapping[str, object]:
    value = decay.get("value")
    return value if isinstance(value, Mapping) else {}


def summarize_health(
    findings: Iterable[RuleFinding | Mapping[str, str]],
    decay: Mapping[str, object] | None = None,
) -> dict:
    """健康度汇总（FR-1.0.24 / E-01）。

    - E-01.1：`score = 100 − 10×error − 3×warn − 8×degraded_decay_dimensions`，
      下限 0（error 很多时不下溢为负）。权重只从 `HEALTH_WEIGHTS` 取（E-01.2）。
    - E-01.3：unknown 维度**不加减分**，但必须把 unknown 数报出来（`unknown_dimensions`），
      UI 据此显示「未知 N 项」。
    - T11 红线：返回值只用于展示与排序，禁止参与放行判定——
      放行只有 GateResult.has_error 一个入口（NFR-10.5）。若把分数接入
      放行逻辑，"调权重即可放行"，治理叙事当场失效（04 §13.13）。
    - NFR-10.3 可下钻：`deductions` 逐条给出扣分项与次数，UI 点击即过滤到对应
      findings / decay 行，禁止只给不可解释的数字。
    - findings 同时接受 RuleFinding 与序列化 dict（daemon / API 路径），
      两种形态同一口径，避免展示层与判定层数量对不上。
    - decay 字段缺失时给 None / 0，不得读 updated_at 之类的 DB 列兜底
      （新鲜度判定只认 git commit 时间，与 T16 同源）。
    """
    error_count = warn_count = info_count = 0
    for f in findings:
        level = f.level if isinstance(f, RuleFinding) else str(f.get("level") or "")
        if level == CheckLevel.ERROR:
            error_count += 1
        elif level == CheckLevel.WARN:
            warn_count += 1
        elif level == CheckLevel.INFO:
            info_count += 1

    decay_map: Mapping[str, object] = decay or {}
    statuses = {dim: _decay_status(decay_map, dim) for dim in DECAY_DIMENSIONS}
    degraded_dims = [dim for dim, status in statuses.items() if status == "degraded"]
    unknown_dims = [dim for dim, status in statuses.items() if status == "unknown"]

    deductions: list[dict] = []
    if error_count:
        deductions.append(
            {"kind": "error", "count": error_count,
             "points": HEALTH_WEIGHTS["error"] * error_count}
        )
    if warn_count:
        deductions.append(
            {"kind": "warn", "count": warn_count,
             "points": HEALTH_WEIGHTS["warn"] * warn_count}
        )
    for dim in degraded_dims:
        deductions.append(
            {"kind": "decay", "dimension": dim, "count": 1,
             "points": HEALTH_WEIGHTS["degraded_decay"]}
        )
    score = max(HEALTH_SCORE_FLOOR, HEALTH_SCORE_MAX - sum(d["points"] for d in deductions))

    value = _decay_value(decay_map)
    staleness_days = decay_map.get("staleness_days")
    if not _is_number(staleness_days):
        staleness_days = value.get("days_since")
    broken = decay_map.get("dependency_broken")
    if isinstance(broken, (list, tuple)):
        broken_count = len(broken)
    else:
        missing = value.get("missing_deps")
        broken_count = len(missing) if isinstance(missing, (list, tuple)) else 0

    return {
        "score": score,
        "error_count": error_count,
        "warn_count": warn_count,
        "info_count": info_count,
        "degraded_decay_dimensions": len(degraded_dims),
        "unknown_dimensions": len(unknown_dims),
        "unknown_dimension_names": unknown_dims,
        "deductions": deductions,
        "staleness_days": staleness_days if _is_number(staleness_days) else None,
        "dependency_broken_count": broken_count,
    }


def run_gate(snapshot: SkillSnapshot, ctx: GateContext) -> GateResult:
    """唯一入口。CLI check / services.push 前置 / 云端 API 全部调用它（NFR-10.5）。"""
    findings: list[RuleFinding] = []
    meta = snapshot.meta
    files = snapshot.files

    # ---- frontmatter 规则 ----
    if not meta.name:
        findings.append(
            RuleFinding(RuleId.NAME_REQUIRED, CheckLevel.ERROR, "frontmatter 缺少 name")
        )
    if not meta.description:
        findings.append(
            RuleFinding(
                RuleId.DESCRIPTION_REQUIRED, CheckLevel.ERROR, "frontmatter 缺少 description"
            )
        )
    if meta.description:
        if not (DESC_MIN_CHARS <= len(meta.description) <= DESC_MAX_CHARS):
            findings.append(
                RuleFinding(
                    RuleId.DESCRIPTION_LENGTH,
                    CheckLevel.WARN,
                    f"description 长度 {len(meta.description)} 不在 "
                    f"{DESC_MIN_CHARS}-{DESC_MAX_CHARS} 区间",
                )
            )
        if estimate_tokens(meta.description) > DESC_TOKEN_HINT:
            findings.append(
                RuleFinding(
                    RuleId.DESCRIPTION_TOKENS,
                    CheckLevel.INFO,
                    f"description 估算约 {estimate_tokens(meta.description)} tokens "
                    f"(> {DESC_TOKEN_HINT})，影响渐进披露",
                )
            )
    if meta.name and not is_kebab(meta.name):
        findings.append(
            RuleFinding(RuleId.NAME_KEBAB, CheckLevel.WARN, f"name {meta.name!r} 不符合 kebab-case")
        )
    if meta.extra:
        unknown = sorted(k for k in meta.extra if k not in _CANONICAL_FM_KEYS)
        if unknown:
            findings.append(
                RuleFinding(
                    RuleId.UNKNOWN_FIELD, CheckLevel.INFO, f"非标准字段: {', '.join(unknown)}"
                )
            )

    # ---- 结构规则 ----
    if not any(f.path == "SKILL.md" for f in files):
        findings.append(
            RuleFinding(RuleId.SKILL_MD_MISSING, CheckLevel.ERROR, "包根缺少 SKILL.md")
        )
    known_paths = {f.path for f in files}
    for ref in _REF.findall(_body_of(files)):
        ref = ref.strip()
        if ref and ref not in known_paths:
            findings.append(
                RuleFinding(RuleId.REF_MISSING, CheckLevel.WARN, f"正文引用的文件不存在: {ref}")
            )
    if any(not f.in_git for f in files):
        untracked = sorted(f.path for f in files if not f.in_git)
        findings.append(
            RuleFinding(
                RuleId.ASSET_UNTRACKED,
                CheckLevel.WARN,
                f"含超阈值未纳管资产（不参与版本管理与漂移检测）: {', '.join(untracked)}",
            )
        )

    # ---- 依赖规则 ----
    for dep in ctx.dependencies:
        if dep not in ctx.known_slugs:
            findings.append(
                RuleFinding(RuleId.DEP_MISSING, CheckLevel.ERROR, f"依赖的 Skill 不在库中: {dep}")
            )
    if ctx.dependency_graph and _has_cycle(ctx.dependency_graph, snapshot.slug):
        findings.append(
            RuleFinding(RuleId.DEP_CYCLE, CheckLevel.WARN, "依赖图存在循环")
        )

    has_error = any(f.level == CheckLevel.ERROR for f in findings)
    return GateResult(findings=findings, has_error=has_error)
