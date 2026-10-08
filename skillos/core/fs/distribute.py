"""分发器（04 §2.2.2，FR-1.0.9 / FR-1.0.26 / FR-1.0.27 / FR-1.0.28 / NFR-3 / NFR-5）。

写入协议（Windows 安全）：

  1 解析目标路径 → 2 归属判定（ReplacePolicy）→ 3 备份到 backups_dir/
  → 4 adapter.render_layout() → 5 写临时目录 .<name>.tmp-<rand>/
  → 6 removals 守卫（比较对象是暂存树）→ 7 暂存哈希校验
  → 8 两步交换（Windows rename 不可覆盖非空目录）
  → 9 部署自证：回读目标逐文件核对哈希（FR-1.0.27）
  → 10 返回新 binding 基线事实（installed_hash 等，由 services 层落库 ——
       本模块在 core，不碰 DB）
  → 11 任一步失败 → 从备份还原

三道安全机制（v1.3 红线，均带变异验证测试）：
- 归属判定：ReplacePolicy 三态无默认值（T13）。RECORDED 要求 disk == installed_hash ——
  这天然放行 OUTDATED（磁盘是我方旧渲染、canonical 已前进），拦下 MODIFIED（用户手改）。
  「SYNCED 是唯一可作证明」的操作化即此判据：能以内容哈希证明磁盘是我方内容才可覆盖。
- removals 守卫：将消失的路径先列出等确认（NFR-11.4 / 11.5：确认绑定精确路径集合；
  无人值守路径由调用方跳过整个 Skill）；比较对象是暂存树而非 canonical 快照（07 D4）。
- 部署自证：写完必须回读核对 ——「我写成功了」与「它在那」是两件事。

跨目标整体拒绝（FR-1.0.28 / T15）：前置全检在写入任何一个之前完成；任一目标不安全
→ 整体拒绝零写入；写阶段任一目标失败 → 已写目标全部回滚。实现约束：Runtime 循环
体内不出现 break / continue（用累积，不用短路）。

dry_run 与真跑共用同一判据（FR-1.3.7）：预览返回的 held_back / 冲突清单必须与真跑
完全一致 —— 另写一套判断会让「预览说没事、真跑删了文件」成为可能。

调用约定：必须在 services 层 write_lock 内调用（FR-1.0.17）；backups_dir 由调用方
显式传入（T1），建议落仓库 .skillos/backups/（.gitignore 排除提交）。
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from skillos.core.algorithm.adapters.base import RuntimeAdapter
from skillos.core.algorithm.hash import aggregate_hash, hash_bytes, list_content_files
from skillos.core.model import SkillSnapshot


class ReplacePolicy(StrEnum):
    """替换授权三态。**无默认值** —— 每个调用点必须显式传参（T13）。

    调用点默认（04 §2.2.2 表）：push=RECORDED · import 换源 / 导出=NO_CLOBBER ·
    rollback=RECORDED · Web 人工覆盖=USER_CONFIRMED。
    """

    NO_CLOBBER = "no_clobber"          # 目标非空即拒，逐字节保留
    RECORDED = "recorded"              # 仅当目标内容 == installed_hash 才替换
    USER_CONFIRMED = "user_confirmed"  # 已由人类显式确认可销毁


@dataclass(frozen=True)
class BindingFacts:
    """runtime_bindings 中与本分发相关的基线事实（services 层取回后传入）。

    installed_hash=None 表示无 binding 记录（首装 / UNMANAGED）。
    """

    installed_hash: str | None = None
    baseline_source: str | None = None  # "distributed" | "imported" | None


@dataclass(frozen=True)
class TargetRequest:
    """一个分发目标：适配器 + 部署根 + 该目标的 binding 基线。"""

    adapter: RuntimeAdapter
    deploy_root: Path
    facts: BindingFacts = field(default_factory=BindingFacts)


@dataclass(frozen=True)
class TargetOutcome:
    """单目标结论。ok=True 且 wrote=False 是幂等跳过；ok=False 是拒绝/失败。"""

    runtime_key: str
    ok: bool
    wrote: bool
    error_code: str | None = None
    held_back_removals: list[str] = field(default_factory=list)
    skipped_assets: list[str] = field(default_factory=list)  # 未纳管资产明细（FR-1.0.20）
    new_installed_hash: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class DistributeReport:
    ok: bool
    outcomes: list[TargetOutcome]
    error_code: str | None = None  # 整体失败时为 PARTIAL_REJECT


@dataclass(frozen=True)
class ResidueReport:
    """残留自检结果（FR-1.0.15）。form: 0=干净 ①=只有 old ②=old+target ③=只有 tmp。

    target 恒为 Skill 部署目录；residue_path 是 ③ 形态下具体的 tmp 目录。
    """

    target: Path
    form: int
    residue_path: Path | None = None


# ---------------------------------------------------------------- 内部计划


@dataclass
class _Plan:
    """前置判定的产物（内部可变）。action ∈ write / skip / conflict / held_back / io。"""

    req: TargetRequest
    target: Path
    desired: dict[str, bytes]
    action: str = "write"
    error_code: str | None = None
    detail: str | None = None
    removals: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)      # 保留的未纳管资产（回收）
    backup: Path | None = None
    swapped: bool = False


def _read_target_file(path: Path) -> bytes:
    """部署自证阶段的回读入口（独立成函数是为了可测试：monkeypatch 污染它模拟写坏）。"""
    return path.read_bytes()


def _collapse_removals(removed: list[str]) -> list[str]:
    """目录只上报一行（templates/ 而非其中每个文件），根级文件报自身（04 步骤 6）。"""
    out: set[str] = set()
    for p in removed:
        out.add(p.split("/", 1)[0] + "/" if "/" in p else p)
    return sorted(out)


def _policy_allows(
    policy: ReplacePolicy, facts: BindingFacts, disk_managed: dict[str, bytes]
) -> bool:
    """归属判定（FR-1.0.26）。只对「目标存在且非空」的目标调用。"""
    if policy is ReplacePolicy.NO_CLOBBER:
        return False
    if policy is ReplacePolicy.RECORDED:
        if facts.installed_hash is None:
            return False  # 无记录 = 无法证明属于我们
        return aggregate_hash(disk_managed) == facts.installed_hash
    return True  # USER_CONFIRMED：人类已确认


def _precheck(
    snapshot: SkillSnapshot,
    req: TargetRequest,
    policy: ReplacePolicy,
    confirmed: frozenset[str] | None,
) -> _Plan:
    """单目标前置判定（无任何写入）。dry_run 与真跑共用同一函数（FR-1.3.7）。"""
    target = req.deploy_root / snapshot.slug
    desired = dict(req.adapter.render_layout(snapshot))
    plan = _Plan(req=req, target=target, desired=desired)

    try:
        disk_exists = target.exists()
        if disk_exists and not target.is_dir():
            plan.action, plan.error_code, plan.detail = "io", "IO", f"目标不是目录: {target}"
            return plan
        entries = list_content_files(target) if disk_exists else []
    except OSError as e:
        # NFR-11.3：无法判断目标内容 → 报错，不许猜（当空目录会漏报 removals）
        plan.action, plan.error_code = "io", "IO"
        plan.detail = f"目标不可读，无法判断其内容: {e}"
        return plan

    disk_managed = {e.path: e.content for e in entries if e.in_git}
    # 未纳管资产：回收保留（同名冲突以 canonical 为准 → 不在 desired 里的才回收）
    plan.untracked = sorted(e.path for e in entries if not e.in_git and e.path not in desired)

    if disk_managed == desired:
        plan.action = "skip"  # NFR-5 幂等：内容一致跳过写入
        return plan

    if disk_exists and entries:
        if not _policy_allows(policy, req.facts, disk_managed):
            plan.action, plan.error_code = "conflict", "TARGET_CONFLICT"
            plan.detail = "无法证明目标属于我方（policy 判定失败），逐字节保留"
            return plan
        # removals 守卫：将消失的纳管路径（暂存树里没有的）
        removed = [p for p in disk_managed if p not in desired]
        plan.removals = _collapse_removals(removed)
        if plan.removals and confirmed != frozenset(plan.removals):
            # NFR-11.5：确认必须绑定精确路径集合；无人值守路径调用方根本不会到这一步
            plan.action, plan.error_code = "held_back", "REMOVALS_REQUIRE_CONFIRM"
            return plan
    return plan


def _outcome(plan: _Plan, *, ok: bool, wrote: bool, error_code: str | None = None,
             detail: str | None = None, new_hash: str | None = None) -> TargetOutcome:
    return TargetOutcome(
        runtime_key=plan.req.adapter.key,
        ok=ok,
        wrote=wrote,
        error_code=error_code,
        held_back_removals=list(plan.removals) if plan.action == "held_back" else [],
        skipped_assets=list(plan.untracked),
        new_installed_hash=new_hash,
        detail=detail,
    )


# ---------------------------------------------------------------- 写入事务


def _backup(plan: _Plan, backups_dir: Path) -> None:
    """步骤 3：写入前备份（进程存活时的失败还原依据；进程被 kill 由残留自检兜底）。"""
    if plan.target.exists():
        plan.backup = backups_dir / plan.target.name / f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"
        plan.backup.mkdir(parents=True, exist_ok=True)
        shutil.copytree(plan.target, plan.backup, dirs_exist_ok=True)


def _stage(plan: _Plan) -> Path:
    """步骤 4–5：渲染进临时目录（含未纳管资产回收），返回 tmp 路径。"""
    tmp = plan.target.with_name(f".{plan.target.name}.tmp-{uuid.uuid4().hex[:8]}")
    plan.target.parent.mkdir(parents=True, exist_ok=True)
    tmp.mkdir()
    for rel, content in plan.desired.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    for rel in plan.untracked:  # 回收未纳管资产（FR-1.0.20）：跨交换保留
        src = plan.target / rel
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, p)
    return tmp


def _verify_staged(tmp: Path, desired: dict[str, bytes], untracked: list[str]) -> None:
    """步骤 7：暂存树哈希校验（写坏盘 / 杀毒拦截在交换前发现）。"""
    for rel, content in desired.items():
        if hash_bytes((tmp / rel).read_bytes()) != hash_bytes(content):
            raise OSError(f"暂存校验失败: {rel}")
    for rel in untracked:
        if not (tmp / rel).is_file():
            raise OSError(f"未纳管资产回收失败: {rel}")


def _swap(plan: _Plan, tmp: Path) -> None:
    """步骤 8：两步交换。Windows rename 不可覆盖非空目录，必须先挪走再换入。"""
    old = plan.target.with_name(plan.target.name + ".old")
    if plan.target.exists():
        if old.exists():
            shutil.rmtree(old)  # 上一次残留，残留自检本应已清；防御性清理
        os.rename(plan.target, old)
        os.rename(tmp, plan.target)
        shutil.rmtree(old)
    else:
        os.rename(tmp, plan.target)
    plan.swapped = True


def _self_verify(plan: _Plan) -> None:
    """步骤 9 部署自证（FR-1.0.27）：回读目标逐文件核对哈希。不一致按失败处理。"""
    for rel, content in plan.desired.items():
        actual = _read_target_file(plan.target / rel)
        if hash_bytes(actual) != hash_bytes(content):
            raise OSError(f"部署自证失败（回读不一致）: {rel}")
    for rel in plan.untracked:
        if not (plan.target / rel).is_file():
            raise OSError(f"未纳管资产在交换后丢失: {rel}")


def _restore(plan: _Plan) -> None:
    """步骤 12：从备份还原（幂等 —— 目标回到分发前状态）。"""
    if plan.target.exists():
        shutil.rmtree(plan.target)
    if plan.backup is not None and plan.backup.exists():
        shutil.copytree(plan.backup, plan.target)


def _attempt_write(plan: _Plan, backups_dir: Path) -> TargetOutcome:
    """单目标写事务：备份 → 暂存 → 校验 → 交换 → 自证；失败还原。"""
    tmp: Path | None = None
    try:
        _backup(plan, backups_dir)
        tmp = _stage(plan)
        _verify_staged(tmp, plan.desired, plan.untracked)
        _swap(plan, tmp)
        _self_verify(plan)
        return _outcome(plan, ok=True, wrote=True, new_hash=aggregate_hash(plan.desired))
    except OSError as e:
        if tmp is not None and tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        _restore(plan)
        plan.swapped = False
        return _outcome(
            plan, ok=False, wrote=False, error_code="VERIFY_FAILED",
            detail=f"写事务失败已还原: {e}",
        )


# ---------------------------------------------------------------- 主入口


def distribute_skill(
    snapshot: SkillSnapshot,
    canonical_commit: str,
    targets: list[TargetRequest],
    *,
    policy: ReplacePolicy,
    backups_dir: Path,
    confirmed_removals: frozenset[str] | None = None,
    dry_run: bool = False,
) -> DistributeReport:
    """把 canonical 快照分发到多个 Runtime 目标（全有或全无）。

    policy 必填（T13）；confirmed_removals 是人工确认可删除的精确路径集合
    （NFR-11.5）；dry_run 只做前置判定，与真跑共用判据、零写入（FR-1.3.7）。
    canonical_commit 仅透传给调用方落库（installed_commit），本模块不碰 git。
    """
    plans = [
        _precheck(snapshot, t, policy, confirmed_removals) for t in targets
    ]

    # 阶段 ②：任一目标不安全 → 整体拒绝零写入（FR-1.0.28）
    unsafe = [p for p in plans if p.action not in ("write", "skip")]
    if dry_run:
        # FR-1.3.7：dry_run 只做前置判定，判据 / 清单与真跑完全一致，零写入
        outcomes = [
            _outcome(
                p,
                ok=(p.action in ("write", "skip")),
                wrote=False,
                error_code=p.error_code if p.action not in ("write", "skip") else None,
                detail=p.detail,
                new_hash=aggregate_hash(p.desired) if p.action in ("write", "skip") else None,
            )
            for p in plans
        ]
        return DistributeReport(ok=all(o.ok for o in outcomes), outcomes=outcomes)
    if unsafe:
        outcomes = [
            _outcome(
                p,
                ok=(p.action == "skip"),
                wrote=False,
                error_code=p.error_code if p.action != "skip" else None,
                detail=p.detail,
                new_hash=aggregate_hash(p.desired) if p.action == "skip" else None,
            )
            for p in plans
        ]
        # 安全目标被牵连：标注 PARTIAL_REJECT（不是它自己的错）
        outcomes = [
            TargetOutcome(
                runtime_key=o.runtime_key,
                ok=False,
                wrote=False,
                error_code="PARTIAL_REJECT",
                held_back_removals=o.held_back_removals,
                skipped_assets=o.skipped_assets,
                new_installed_hash=o.new_installed_hash,
                detail="其它目标不安全，整体拒绝（FR-1.0.28）",
            )
            if p.action in ("write", "skip")
            else o
            for p, o in zip(plans, outcomes, strict=True)
            ]
        return DistributeReport(ok=False, outcomes=outcomes, error_code="PARTIAL_REJECT")

    # 阶段 ③：写事务。循环体内无 break / continue（T15）—— 失败靠累积 + 事后整体回滚
    results: list[tuple[_Plan, TargetOutcome]] = []
    for p in plans:
        if p.action == "skip":
            results.append(
                (p, _outcome(p, ok=True, wrote=False, new_hash=aggregate_hash(p.desired)))
            )
        elif p.action == "write":
            results.append((p, _attempt_write(p, backups_dir)))

    failed = [o for _, o in results if not o.ok]
    if not failed:
        return DistributeReport(ok=True, outcomes=[o for _, o in results])

    # 写阶段失败 → 已写成功的目标全部回滚（NFR-3 全有或全无）
    rolled: list[TargetOutcome] = []
    for p, o in results:
        if o.ok and o.wrote:
            _restore(p)
            rolled.append(
                TargetOutcome(
                    runtime_key=o.runtime_key, ok=False, wrote=False,
                    error_code="PARTIAL_REJECT",
                    skipped_assets=o.skipped_assets,
                    detail="另一目标写失败，整体回滚（FR-1.0.28）",
                )
            )
        else:
            rolled.append(o)
    return DistributeReport(ok=False, outcomes=rolled, error_code="PARTIAL_REJECT")


# ---------------------------------------------------------------- 残留自检（FR-1.0.15）


def check_residue(target: Path) -> ResidueReport:
    """三种残留形态判定（幂等）。target 是 Skill 部署目录路径。"""
    if not target.parent.is_dir():
        return ResidueReport(target=target, form=0)
    old = target.with_name(target.name + ".old")
    tmps = [
        p
        for p in target.parent.iterdir()
        if p.is_dir() and p.name.startswith(f".{target.name}.tmp-")
    ]
    if old.exists() and not target.exists():
        return ResidueReport(target=target, form=1)  # 交换未完成
    if old.exists() and target.exists():
        return ResidueReport(target=target, form=2)  # 交换完成仅剩清理
    if tmps:
        return ResidueReport(target=target, form=3, residue_path=tmps[0])  # 写临时目录中断
    return ResidueReport(target=target, form=0)


def recover_residue(report: ResidueReport) -> None:
    """按形态恢复（幂等，可重复执行）：
    ① 改回（回退到分发前）② 删 old 补清理 ③ 删 tmp 残留。"""
    target = report.target
    if not target.parent.is_dir():
        return
    old = target.with_name(target.name + ".old")
    if old.exists() and not target.exists():
        os.rename(old, target)
    elif old.exists():
        shutil.rmtree(old)
    for p in target.parent.iterdir():
        if p.is_dir() and p.name.startswith(f".{target.name}.tmp-"):
            shutil.rmtree(p)
