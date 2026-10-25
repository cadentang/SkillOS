"""质量检查编排（FR-2.2.9 / E-02.6）。

分层边界（`docs/12` §1 模块落位原则）：判定一律在 `core/algorithm`（checks / decay），
本层只做三件事 —— **取证 → 调纯函数 → 落库**。`engine.py` 已近 1900 行，
折旧的新逻辑落这里，不再往 engine 里加。

取证口径（E-02.1 / E-02.3）：
- `days_since_last_commit`：canonical 里该 skill 目录最近一次 commit 的 UTC 时间
  （**不读** DB `updated_at`，E-02.7 / T16）
- `inbound_refs`：`skill_dependencies` 反向入度 + 其他 skill 正文里的相对路径引用
- `declared_deps`：frontmatter `dependencies`（历史字段 `requires` 回落）
- `dependency_bindings_ok`：依赖在本 skill binding 所在 Runtime 上是否有 synced binding
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from skillos.core.algorithm.checks import (
    CheckLevel,
    GateContext,
    GateResult,
    run_gate,
)
from skillos.core.algorithm.decay import (
    FORMAT_UNKNOWN,
    DecayResult,
    DeclaredDeps,
    FormatUnknown,
    evaluate_decay,
)
from skillos.core.algorithm.rule_engine import (
    RuleRunner,
    filter_security,
    finding_rows,
)
from skillos.core.algorithm.rule_engine import (
    invalid_rows as invalid_rule_rows,
)
from skillos.core.model import FileEntry, SkillSnapshot

if TYPE_CHECKING:
    from skillos.services.rule_service import RuleService

# 正文里指向同库另一个 skill 的相对路径：`../<slug>/`（从 skills/<slug>/ 出发）
# 或 `skills/<slug>/`（从仓库根出发）。只认 slug 字形，避免把任意目录当成 skill。
_REF_RE = re.compile(r"(?:\.\./|skills/)([a-z0-9][a-z0-9-]*)/")

DECAY_SCOPE = "decay"
GATE_SCOPE = "gate"

# 三维状态 → skill_checks.level（E-01.3：「未知 N 项」= scope=decay 且 level=unknown 的行数）
_STATUS_TO_LEVEL: dict[str, str] = {
    "ok": "info",
    "degraded": "warn",
    "unknown": "unknown",
}


def _body_of(snapshot: SkillSnapshot) -> str:
    for f in snapshot.files:
        if f.path == "SKILL.md":
            try:
                return f.content.decode("utf-8")
            except UnicodeDecodeError:
                return ""
    return ""


def declared_deps(snapshot: SkillSnapshot) -> DeclaredDeps:
    """frontmatter 依赖声明（E-02.3）。

    口径：`dependencies: [slug, ...]`。历史数据写的是 `requires`，**只有在前者缺失时**
    才回落读取，避免既有的 62 个 skill 被静默当成「无依赖」；两者同在时以 `dependencies`
    为准。字段存在但读不懂（非 list / 元素非 str）→ `FORMAT_UNKNOWN`，不猜（E-02.4）。
    """
    extra = snapshot.meta.extra or {}
    raw = extra.get("dependencies")
    if raw is None:
        raw = extra.get("requires")
    if raw is None:
        return []
    if not isinstance(raw, list):
        return FORMAT_UNKNOWN
    if not all(isinstance(item, str) for item in raw):
        return FORMAT_UNKNOWN
    return [item.strip() for item in raw if item.strip()]


@dataclass(frozen=True)
class ReferenceIndex:
    """全库引用预扫描结果（E-02.1 ②）。

    一次 O(n) 扫描拿全量入度，**不按 skill 重扫全库**（62 skill 逐个全扫 = O(n²)）。
    同一引用方在多处提到同一 skill 只算 1 次 —— 否则一处反复提及就能把
    「被引用」吹到放宽阈值，等于把判据玩坏。
    """

    inbound: dict[str, int]

    @classmethod
    def build(
        cls,
        bodies: Mapping[str, str],
        dep_edges: Iterable[tuple[str, str]],
        slugs: Iterable[str],
    ) -> ReferenceIndex:
        known = set(slugs)
        referenced_by: dict[str, set[str]] = {}
        for owner, body in bodies.items():
            for target in set(_REF_RE.findall(body)):
                if target != owner and target in known:
                    referenced_by.setdefault(target, set()).add(owner)

        inbound: dict[str, int] = {slug: len(referenced_by.get(slug, ())) for slug in known}
        for owner, dep in set(dep_edges):
            if dep in known and dep != owner:
                inbound[dep] = inbound.get(dep, 0) + 1
        return cls(inbound=inbound)

    def of(self, slug: str) -> int:
        return self.inbound.get(slug, 0)


class CheckService:
    """检查编排：内置门禁 + 自定义规则 + 折旧，取证 → 判定 → 落库（一个入口）。

    `docs/12` §6.1：判定仍全在 `core/algorithm`（checks / rule_engine / decay），
    本层只负责把它们拼起来、过滤白名单、写 `skill_checks`。
    """

    def __init__(self, *, git, store, head: Callable[[], str],
                 rules: RuleService | None = None,
                 gate_context: Callable[[SkillSnapshot], GateContext] | None = None,
                 extra_files: Callable[[str], Iterable[FileEntry]] | None = None) -> None:
        self.git = git
        self.store = store
        self._head = head
        self._snapshots: dict[str, SkillSnapshot] | None = None
        self._references: ReferenceIndex | None = None
        self.rules = rules
        self._gate_context = gate_context
        self._extra_files = extra_files
        self._runner = RuleRunner()

    # ---------------------------------------------------------------- 门禁（含自定义规则）

    def gate(self, snapshot: SkillSnapshot, ctx: GateContext, *,
             security_only: bool = False,
             include_untracked: bool = False) -> GateBundle:
        """内置 11 条 + 自定义规则，唯一判定入口（NFR-10.5 / N-03.7）。

        自定义 error **参与拦截**（与内置同语义）；ignore 命中的不产出 finding，
        但会作为 info 行留痕（N-08.2 可审计）。
        """
        base = run_gate(snapshot, ctx)
        findings = list(base.findings)
        invalid_rows: list[dict] = []
        ignored_rows: list[dict] = []
        custom_count = 0
        ruleset_hash = ""

        if self.rules is not None:
            loaded = self.rules.load()
            ruleset_hash = self.rules.ruleset_hash()
            # 编译失败的规则：info 落库，不静默跳过（N-03.4）
            invalid_rows = invalid_rule_rows(loaded.invalid)
            rules = list(loaded.rules)
            if security_only:
                rules = filter_security(rules)
            extra: tuple[FileEntry, ...] = ()
            if include_untracked and self._extra_files is not None:
                extra = tuple(self._extra_files(snapshot.slug))
            custom = self._runner.run(rules, snapshot, extra_files=extra)
            custom_count = len(custom)
            for item in custom:
                hit = self.rules.ignored(item.rule, item.path or "")
                if hit is not None:
                    ignored_rows.append({
                        "rule": item.rule,
                        "level": "info",
                        "message": f"已按白名单过滤（{hit.reason}）：{item.message}",
                        "value": None,
                    })
                    continue
                findings.append(item)

        result = GateResult(
            findings=findings,
            has_error=any(f.level == CheckLevel.ERROR for f in findings),
        )
        return GateBundle(result=result, ruleset_hash=ruleset_hash,
                          invalid=invalid_rows, ignored=ignored_rows,
                          custom_count=custom_count)

    def _snapshot_of(self, slug: str) -> SkillSnapshot:
        snapshot = self._all_snapshots().get(slug)
        if snapshot is None:
            raise ValueError(f"Skill 不在 canonical 中: {slug}")
        return snapshot

    def _context(self, snapshot: SkillSnapshot) -> GateContext:
        if self._gate_context is not None:
            return self._gate_context(snapshot)
        return GateContext()

    # ---------------------------------------------------------------- 取证

    def _all_snapshots(self) -> dict[str, SkillSnapshot]:
        """一次批量快照（1 ls-tree + 1 cat-file --batch），会话内复用。

        逐 skill 调 snapshot_at 是 N×(1+M) 次 git spawn，Windows 上 20s+（core/fs/version.py）。
        """
        if self._snapshots is None:
            self._snapshots = self.git.snapshot_all(self._head())
        return self._snapshots

    def references(self) -> ReferenceIndex:
        if self._references is None:
            snapshots = self._all_snapshots()
            self._references = ReferenceIndex.build(
                bodies={slug: _body_of(snap) for slug, snap in snapshots.items()},
                dep_edges=self.store.list_dependency_edges(),
                slugs=snapshots,
            )
        return self._references

    def _days_since_last_commit(self, slug: str) -> float | None:
        """距该 skill 目录最近一次 commit 的天数。无历史 → None（→ unknown，不猜）。"""
        commits = self.git.log(path=f"skills/{slug}", limit=1)
        if not commits:
            return None
        last = datetime.fromisoformat(commits[0].at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return (datetime.now(UTC) - last).total_seconds() / 86400

    def _dependency_bindings_ok(
        self, slug: str, declared: DeclaredDeps, known: set[str]
    ) -> dict[str, bool]:
        """每个依赖在本 skill binding 所在 Runtime 上是否有有效（synced）binding。

        判不出来就不给键 —— decay 侧会转成 unknown，而不是默认成 ok（NFR-10.4）。
        """
        if isinstance(declared, FormatUnknown) or not declared:
            return {}
        record = self.store.get_skill_by_slug(slug)
        if record is None:
            return {}
        parent_runtimes = {b.runtime_id for b in self.store.list_bindings(record.id)}
        if not parent_runtimes:
            return {}

        result: dict[str, bool] = {}
        for dep in declared:
            if dep not in known:
                continue  # 缺失由 decay 判 degraded，这里不给判据
            dep_record = self.store.get_skill_by_slug(dep)
            if dep_record is None:
                continue
            synced = {
                b.runtime_id
                for b in self.store.list_bindings(dep_record.id)
                if b.status == "synced"
            }
            result[dep] = parent_runtimes.issubset(synced)
        return result

    # ---------------------------------------------------------------- 判定

    def decay(self, slug: str) -> DecayResult:
        """算一个 skill 的折旧三维（判定全在 core/algorithm/decay）。"""
        snapshots = self._all_snapshots()
        snapshot = snapshots.get(slug)
        if snapshot is None:
            raise ValueError(f"Skill 不在 canonical 中: {slug}")
        declared = declared_deps(snapshot)
        known = set(snapshots)
        # E-08.3：有 upstream_sources 记录才参与上游维度；没有记录 → None → unknown
        # （不按"没记录 = 最新"猜，NFR-10.4）
        record = self.store.get_skill_by_slug(slug)
        source = None if record is None else self.store.get_upstream_source(record.id)
        upstream_status = None if source is None else source.status
        result = evaluate_decay(
            days_since_last_commit=self._days_since_last_commit(slug),
            inbound_refs=self.references().of(slug),
            declared_deps=declared,
            resolved_slugs=known,
            dependency_bindings_ok=self._dependency_bindings_ok(slug, declared, known),
            upstream_status=upstream_status,
        )
        if result.value.get("clock_skew"):
            # 时钟回拨按 0 处理（E-02 边界）；纯函数不记日志，告警在这里落地
            from skillos.core.logging import get_logger

            get_logger(__name__).warning("decay_clock_skew", skill_slug=slug)
        return result

    def gate_of(self, slug: str, *, security_only: bool = False,
                include_untracked: bool = False) -> GateBundle:
        """单 skill 门禁（含自定义规则）：给 CLI / push 前置 / 详情页用。"""
        snapshot = self._snapshot_of(slug)
        return self.gate(snapshot, self._context(snapshot),
                         security_only=security_only,
                         include_untracked=include_untracked)

    def check(self, slug: str, *, security_only: bool = False,
              include_untracked: bool = True) -> CheckReport:
        """一次完整检查：门禁（含自定义规则）+ 折旧，两者都落库。

        `include_untracked=True` 是 N-08.3 的显式例外：超阈值未纳管资产
        （`in_git=false`，字节不进哈希）**仍参与安全扫描** —— 密钥藏在大文件里
        同样要拦，代价是显式 check 时会多读一次磁盘（push 前置不传这个开关）。
        """
        snapshot = self._snapshot_of(slug)
        bundle = self.gate(snapshot, self._context(snapshot),
                           security_only=security_only,
                           include_untracked=include_untracked)
        self.record_gate(slug, bundle)
        decay = self.decay(slug)
        self.record_decay(slug, decay)
        return CheckReport(gate=bundle.result, decay=decay, bundle=bundle)

    # ---------------------------------------------------------------- 落库

    def record_gate(self, slug: str, bundle: GateBundle) -> int:
        """写 skill_checks(scope=gate)：findings + 无效规则 + 白名单留痕，一次替换。"""
        record = self.store.get_skill_by_slug(slug)
        if record is None:
            return 0
        rows = finding_rows(bundle.result.findings) + bundle.invalid + bundle.ignored
        return self.store.replace_gate_checks(
            record.id, rows, ruleset_hash=bundle.ruleset_hash or None,
        )

    def record_decay(self, slug: str, result: DecayResult) -> int:
        """写 skill_checks(scope=decay)：一行一个维度（E-02.6），返回写入行数。"""
        record = self.store.get_skill_by_slug(slug)
        if record is None:
            return 0
        return self.store.replace_decay_checks(record.id, decay_rows(result.to_dict()))


@dataclass(frozen=True)
class GateBundle:
    """一次门禁的完整产物：内置 + 自定义 findings、规则集指纹、无效规则、白名单留痕。"""

    result: GateResult
    ruleset_hash: str
    invalid: list[dict]
    ignored: list[dict]
    custom_count: int


@dataclass(frozen=True)
class CheckReport:
    """一次完整检查的结果（CLI / daemon op / 详情页共用）。"""

    gate: GateResult
    decay: DecayResult
    bundle: GateBundle


def decay_rows(payload: Mapping[str, object]) -> list[dict]:
    """把 decay 结果转成 skill_checks(scope=decay) 的行（E-02.6 / E-01.3）。

    兼容两代键名：本地 daemon 输出用 `dependency_broken_status`（旧字段
    `dependency_broken` 仍是 slug 数组给前端），`DecayResult.to_dict()` 用 `dependency_broken`。
    状态读不出来一律按 unknown 落库 —— 与判定侧的「不猜」同一条原则。
    """
    raw_value = payload.get("value")
    value: Mapping[str, object] = raw_value if isinstance(raw_value, Mapping) else {}
    value_json = json.dumps(value, ensure_ascii=False, sort_keys=True)

    rows: list[dict] = []
    for dimension in ("staleness", "dependency_broken", "upstream_divergence"):
        raw = payload.get(f"{dimension}_status")
        if not isinstance(raw, str):
            candidate = payload.get(dimension)
            raw = candidate if isinstance(candidate, str) else "unknown"
        status = raw if raw in _STATUS_TO_LEVEL else "unknown"
        rows.append({
            "rule": dimension,
            "level": _STATUS_TO_LEVEL[status],
            "message": _decay_message(dimension, status, value),
            "value": value_json,
        })
    return rows


def _decay_message(dimension: str, status: str, value: Mapping[str, object]) -> str:
    """给 UI 看的一句话（与 value 里的原始值一一对应，NFR-10.3 可下钻）。"""
    if dimension == "staleness":
        if status == "unknown":
            return "陈旧度未知：没有可用的 commit 历史"
        if status == "degraded":
            relaxed = "（被引用已放宽阈值）" if value.get("relaxed_by_refs") else ""
            return f"陈旧：距上次提交 {value.get('days_since')} 天{relaxed}"
        return f"新鲜：距上次提交 {value.get('days_since')} 天"
    if dimension == "dependency_broken":
        if status == "unknown":
            if value.get("gate_signal"):
                return "依赖声明格式无法解析（需要 dependencies: [slug, ...]）"
            return "依赖状态未知：缺少可判定的 binding 信息"
        missing = value.get("missing_deps") or []
        unbound = value.get("unbound_deps") or []
        if status == "degraded":
            parts = []
            if missing:
                parts.append(f"库内缺失 {', '.join(missing)}")
            if unbound:
                parts.append(f"目标 Runtime 无有效副本 {', '.join(unbound)}")
            return "依赖失效：" + "；".join(parts)
        return "依赖正常"
    if status == "unknown":
        return "上游分叉未知：E-08 上游跟踪未启用"
    return f"上游状态 {value.get('upstream_status')}"
