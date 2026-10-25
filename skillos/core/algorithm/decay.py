"""折旧三维判定（FR-1.0.23 / E-02）。纯函数，无 IO、无全局状态。

三个维度（`docs/11` E-02.1~E-02.5）：

| 维度 | 判据 |
|---|---|
| `staleness` | 距 canonical 最近一次 commit 的天数 vs 阈值（被引用 ≥1 次放宽一档） |
| `dependency_broken` | 声明依赖在库内缺失，或在 binding 所在 Runtime 无有效 binding |
| `upstream_divergence` | E-08 上游跟踪启用前恒 unknown |

三维状态集统一为 `ok / degraded / unknown`（无 warn 档，E-02.2）。

**不猜原则（NFR-10.4）**：输入缺失或无法解析时一律 `unknown`，不做任何"给个默认值"
的兜底。unknown 在健康度合成里不加减分（E-01.3），但必须能报出来。

**T16 禁令**：新鲜度只认 canonical 的 git commit 时间。本模块的 `days_since_last_commit`
由调用方按 git 取证后传入，**不接受** DB `updated_at` 之类的时间戳（E-02.7）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from skillos.core.constants import (
    STALENESS_DEGRADED_DAYS,
    STALENESS_DEGRADED_DAYS_RELAXED,
)


class DecayStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class FormatUnknown:
    """frontmatter `dependencies` 无法解析时的哨兵值（E-02.4）。

    与"空依赖"严格区分：空列表是"确实没有依赖"（ok），本哨兵是"看见了但读不懂"（unknown）。
    两者混用会让一个格式写坏的 Skill 被静默当成健康资产 —— 那正是 NFR-10.4 要防的事。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # 便于失败信息定位
        return "FORMAT_UNKNOWN"


FORMAT_UNKNOWN = FormatUnknown()

DeclaredDeps = list[str] | FormatUnknown

# gate 层信号名（E-02.4）：由 check 服务转成一条 info finding
GATE_SIGNAL_DEPS_FORMAT = "fm.dependencies.format"

# E-08.3 映射表：upstream_sources.status（current/outdated/error/unknown）为权威取值；
# 前半批旧别名保留，避免历史上打过交道的调用点静默变成 unknown。
_UPSTREAM_STATUS_MAP: dict[str, DecayStatus] = {
    "current": DecayStatus.OK,      # 上游无更新；本地 fork 也是事实，不算退化
    "outdated": DecayStatus.DEGRADED,  # 上游已前进 → 需要跟进
    "error": DecayStatus.UNKNOWN,   # 连续失败：不知道上游啥情况，不猜
    "unknown": DecayStatus.UNKNOWN,
    "ok": DecayStatus.OK,
    "synced": DecayStatus.OK,
    "up_to_date": DecayStatus.OK,
    "diverged": DecayStatus.DEGRADED,
    "divergence": DecayStatus.DEGRADED,
    "behind": DecayStatus.DEGRADED,
}


@dataclass(frozen=True)
class DecayResult:
    """三维状态 + 原始测量值。

    `value` 会原样落进 `skill_checks(scope=decay)` 的 JSON 列供 UI 下钻（E-02.6），
    所以键名是对外契约：只加不改。
    """

    staleness: DecayStatus = DecayStatus.UNKNOWN
    dependency_broken: DecayStatus = DecayStatus.UNKNOWN
    upstream_divergence: DecayStatus = DecayStatus.UNKNOWN
    value: dict[str, object] = field(default_factory=dict)

    def dimensions(self) -> dict[str, DecayStatus]:
        return {
            "staleness": self.staleness,
            "dependency_broken": self.dependency_broken,
            "upstream_divergence": self.upstream_divergence,
        }

    def degraded_dimensions(self) -> list[str]:
        return [k for k, v in self.dimensions().items() if v == DecayStatus.DEGRADED]

    def unknown_dimensions(self) -> list[str]:
        return [k for k, v in self.dimensions().items() if v == DecayStatus.UNKNOWN]

    def to_dict(self) -> dict:
        """序列化给 daemon / API / 落库用（与 checks.summarize_health 的入参形态一致）。"""
        return {
            **{k: str(v) for k, v in self.dimensions().items()},
            "value": dict(self.value),
        }


def evaluate_staleness(
    days_since_last_commit: int | float | None,
    inbound_refs: int,
) -> tuple[DecayStatus, dict]:
    """E-02.1 / E-02.2：陈旧判定。被引用 ≥1 次的资产不算僵尸，阈值放宽一档。"""
    if days_since_last_commit is None:
        # 没有 commit 历史（新库 / 未纳管）—— 不知道就别判，E-02.1
        return DecayStatus.UNKNOWN, {"days_since": None, "inbound_refs": inbound_refs}

    days = float(days_since_last_commit)
    clock_skew = days < 0
    if clock_skew:
        days = 0.0   # 时钟回拨按 0 处理，告警由调用方按 value.clock_skew 打（E-02 边界）

    relaxed = inbound_refs >= 1
    threshold = STALENESS_DEGRADED_DAYS_RELAXED if relaxed else STALENESS_DEGRADED_DAYS
    value: dict = {
        "days_since": days if days != int(days) else int(days),
        "inbound_refs": inbound_refs,
        "threshold_days": threshold,
        "relaxed_by_refs": relaxed,
    }
    if clock_skew:
        value["clock_skew"] = True
    status = DecayStatus.DEGRADED if days >= threshold else DecayStatus.OK
    return status, value


def evaluate_dependency(
    declared_deps: DeclaredDeps,
    resolved_slugs: set[str] | frozenset[str],
    dependency_bindings_ok: dict[str, bool],
) -> tuple[DecayStatus, dict]:
    """E-02.3 / E-02.4：依赖断裂判定，两层。

    ① 声明的 slug 在库内不存在 → degraded；
    ② 库内存在、但该 slug 在本 skill binding 所在 Runtime 上没有有效 binding → degraded。
    声明格式读不懂 → unknown，并回一条 gate 信号（不猜，E-02.4）。
    """
    if isinstance(declared_deps, FormatUnknown):
        return DecayStatus.UNKNOWN, {
            "declared_deps": None,
            "missing_deps": [],
            "unbound_deps": [],
            "gate_signal": GATE_SIGNAL_DEPS_FORMAT,
        }

    declared = list(declared_deps)
    missing = [d for d in declared if d not in resolved_slugs]
    value: dict = {"declared_deps": declared, "missing_deps": missing, "unbound_deps": []}
    if not declared:
        return DecayStatus.OK, value            # 确实没有依赖
    if missing:
        return DecayStatus.DEGRADED, value      # 第 ① 层

    unbound = [d for d in declared if dependency_bindings_ok.get(d) is False]
    value["unbound_deps"] = unbound
    if unbound:
        return DecayStatus.DEGRADED, value      # 第 ② 层

    undetermined = [d for d in declared if d not in dependency_bindings_ok]
    if undetermined:
        # 调用方没能给出判据（例如该 skill 没有任何 binding 可比）—— 不知道，不猜
        value["undetermined_deps"] = undetermined
        return DecayStatus.UNKNOWN, value
    return DecayStatus.OK, value


def evaluate_upstream(upstream_status: str | None) -> tuple[DecayStatus, dict]:
    """E-02.5：E-08 启用前恒 unknown；启用后按 E-08.3 映射。"""
    if upstream_status is None:
        return DecayStatus.UNKNOWN, {"upstream_status": None}
    mapped = _UPSTREAM_STATUS_MAP.get(str(upstream_status).strip().lower(), DecayStatus.UNKNOWN)
    return mapped, {"upstream_status": str(upstream_status)}


def evaluate_decay(
    *,
    days_since_last_commit: int | float | None,
    inbound_refs: int,
    declared_deps: DeclaredDeps,
    resolved_slugs: set[str] | frozenset[str],
    dependency_bindings_ok: dict[str, bool],
    upstream_status: str | None,
) -> DecayResult:
    """折旧三维统一入口（E-02 全量口径）。调用方只负责取证，判定全部在此。"""
    staleness, s_value = evaluate_staleness(days_since_last_commit, inbound_refs)
    dependency, d_value = evaluate_dependency(declared_deps, resolved_slugs, dependency_bindings_ok)
    upstream, u_value = evaluate_upstream(upstream_status)
    return DecayResult(
        staleness=staleness,
        dependency_broken=dependency,
        upstream_divergence=upstream,
        # 三维原始值平铺在一个 dict 里落库（E-02.6：供 UI 下钻）
        value={**s_value, **d_value, **u_value},
    )
