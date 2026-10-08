"""本地引擎编排层（04 §2.2 / §5.2 push 时序）。

CLI / daemon / MCP 三个入口共用这一层（分层红线：入口 → services → core/store）。
本层职责：锁（FR-1.0.17，网络 I/O 永不进锁内 T17）+ 门禁（FR-1.0.11，唯一判定
在 core/algorithm/checks，NFR-10.5）+ 分发（core/fs/distribute）+ 基线落库（store）。

关键语义：
- imported 基线：installed_hash 记录**纳管时的磁盘内容**（证明磁盘未被手改的锚点），
  首次 push 成功后转 distributed（04 §2.2.5）。
- push 前对该 skill 各目标做残留定向检查（FR-1.0.15）。
- 「不适用」（磁盘无目录且无 binding）的格子不进漂移矩阵（04 §2.1.3）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy.orm import Session

from skillos.core.algorithm.adapters.agents import AgentsAdapter
from skillos.core.algorithm.adapters.base import RuntimeAdapter
from skillos.core.algorithm.adapters.claude import ClaudeAdapter
from skillos.core.algorithm.adapters.codex import CodexAdapter
from skillos.core.algorithm.adapters.workbuddy import WorkBuddyAdapter
from skillos.core.algorithm.checks import GateContext, GateResult, is_kebab, run_gate
from skillos.core.algorithm.drift import classify, classify_imported
from skillos.core.algorithm.hash import aggregate_hash, list_content_files
from skillos.core.algorithm.identity import IdentityCandidate
from skillos.core.fs.distribute import (
    BindingFacts,
    DistributeReport,
    ReplacePolicy,
    TargetOutcome,
    TargetRequest,
    check_residue,
    distribute_skill,
    recover_residue,
)
from skillos.core.fs.importer import ImportPlan, plan_import
from skillos.core.fs.safe_path import path_key, safe_path
from skillos.core.fs.scan import scan_roots
from skillos.core.fs.version import GitError, GitRepo
from skillos.core.model import DriftReport, DriftState, RootSpec
from skillos.services.locking import write_lock
from skillos.store.db import init_schema, make_engine, make_session_factory, sqlite_url
from skillos.store.index_repo import SkillQuery, SkillRecord, SqliteIndexStore
from skillos.store.models import RuntimeBinding


class ConflictDecision(StrEnum):
    """CONFLICT 决策枚举（FR-2.2.6 / RED LINE 12）。

    四选一，整文件粒度，不做块内合并：
    - keep_local  保留本地：磁盘内容成为 canonical 新版本（同 pull_skill）
    - keep_remote 保留仓库：用 canonical HEAD 覆盖 runtime 磁盘（USER_CONFIRMED policy）
    - keep_both   两个都保留：磁盘 → *.local 后缀 + canonical → 磁盘
                   ※ 仅 Web 人工路径可达（MCP 工具参数中不得出现，T22）
    - defer       暂不处理：不动内容，只落 sync_records 标记 deferred
    """

    KEEP_LOCAL = "keep_local"
    KEEP_REMOTE = "keep_remote"
    KEEP_BOTH = "keep_both"
    DEFER = "defer"


_VALID_DECISIONS: frozenset[str] = frozenset(d.value for d in ConflictDecision)

# 漂移严重度（FR-2.1.9：按 Skill 聚合取组内最严重）
_SEVERITY: dict[DriftState, int] = {
    DriftState.SYNCED: 0,
    DriftState.MISSING: 1,
    DriftState.ORPHANED: 2,
    DriftState.UNMANAGED: 3,
    DriftState.OUTDATED: 4,
    DriftState.MODIFIED: 5,
    DriftState.CONFLICT: 6,
}


def all_adapters() -> list[RuntimeAdapter]:
    """四个内置 Runtime 适配器（新增 Runtime 只加数据，T12）。"""
    return [ClaudeAdapter(), AgentsAdapter(), WorkBuddyAdapter(), CodexAdapter()]


@dataclass(frozen=True)
class EnvPaths:
    """本地环境路径（T1：全部显式；沙盒下由调用方改指向容器内目录）。"""

    repo_root: Path
    db_path: Path

    @property
    def backups_dir(self) -> Path:
        return self.repo_root / ".skillos" / "backups"

    @property
    def lock_dir(self) -> Path:
        return self.db_path.parent


def default_env(home: Path) -> EnvPaths:
    state = home / ".skillos"
    return EnvPaths(repo_root=state / "repo", db_path=state / "skillos.db")


def resolve_home() -> Path:
    """入口层 home 解析（T1 / FR-2.4.3）：SKILLOS_ROOT 指向容器内虚拟目录。

    沙盒隔离靠把 root 全部指向容器内实现 —— 本函数只在入口层
    （CLI / daemon / doctor）读环境变量并显式传参，core/fs 全程无感知。
    """
    root = os.environ.get("SKILLOS_ROOT")
    return Path(root) if root else Path.home()


class LocalEngine:
    """引擎闭环：init → scan → import → status → push → doctor。"""

    def __init__(self, paths: EnvPaths, home: Path, session: Session | None = None,
                 sandbox: bool | None = None) -> None:
        self.paths = paths
        self.home = home
        # 沙盒模式（FR-2.4.3 / FR-3.10）：None = 按环境变量判定（入口层约定）。
        # is_sandbox 只控制 sandbox_reset 的可达性，不隐藏任何写操作。
        self.is_sandbox = (
            sandbox if sandbox is not None
            else os.environ.get("SKILLOS_SANDBOX") == "1"
        )
        self.git = GitRepo(paths.repo_root)
        if session is None:
            paths.db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = make_engine(sqlite_url(paths.db_path))
            init_schema(engine)
            session = make_session_factory(engine)()
        self.db = SqliteIndexStore(session)

    # ------------------------------------------------------------ init

    def init_env(self) -> list[str]:
        """初始化 git 仓库 + SQLite + runtimes 表。幂等。"""
        self.paths.repo_root.mkdir(parents=True, exist_ok=True)
        self.paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not (self.paths.repo_root / ".git").exists():
            self.git.init()
        for adapter in all_adapters():
            root = adapter.resolve_root("user", home=self.home)
            root.mkdir(parents=True, exist_ok=True)  # doctor 第 7 项：四端可达可写
            self.db.upsert_runtime(
                runtime_id=adapter.key, key=adapter.key,
                adapter_key=adapter.key, root_path=root, scope="user",
            )
        return ["git 仓库就绪", "SQLite 索引就绪（缓存，可重建）", "runtimes 已登记四端部署根"]

    # ------------------------------------------------------------ scan / import

    def _root_specs(self) -> list[RootSpec]:
        specs: list[RootSpec] = []
        for adapter in all_adapters():
            for root in adapter.scan_roots("user", home=self.home):
                specs.append(RootSpec(runtime_key=adapter.key, root=root, scope="user"))
        return specs

    def scan_all(self) -> list:
        return scan_roots(self._root_specs())

    def _known_candidates(self) -> list[IdentityCandidate]:
        known: list[IdentityCandidate] = []
        for row in self.db.list_skills(SkillQuery(limit=10000)):
            binding_hash = None
            for b in self.db.list_bindings(row.id):
                if b.installed_hash:
                    binding_hash = b.installed_hash
                    break
            known.append(IdentityCandidate(
                skill_id=row.id, source_ref=None,
                content_hash=binding_hash, dir_name=row.slug,
            ))
        return known

    def import_skills(self, runtimes: list[str] | None = None) -> ImportPlan:
        """纳管（04 §2.2.5）。canonical = 主副本字节级拷贝（不归一化）；不自动 push。"""
        scanned = [s for s in self.scan_all() if runtimes is None or s.runtime_key in runtimes]
        plan = plan_import(scanned, self._known_candidates())
        with write_lock(self.paths.lock_dir):
            for planned in plan.skills:
                primary_dir = planned.primary.root / planned.primary.rel_path
                self._commit_canonical(planned.slug, primary_dir)
                self._record_import_binding(planned)
        return plan

    def _commit_canonical(self, slug: str, skill_dir: Path) -> None:
        """主副本字节级拷入 repo skills/<slug>/ 并提交。已存在则幂等跳过。"""
        dest = self.paths.repo_root / "skills" / slug
        if dest.exists():
            return
        wrote = False
        for e in list_content_files(skill_dir):
            if not e.in_git:
                continue  # 超阈值资产不进 canonical（T2）
            p = dest / e.path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(e.content)
            wrote = True
        if wrote:
            self.git.commit([f"skills/{slug}"], f"skillos: import {slug}")

    def _record_import_binding(self, planned) -> None:
        meta = planned.primary.meta
        record = self.db.upsert_skill(SkillRecord(
            id=planned.skill_id or "", slug=planned.slug,
            name=(meta.name if meta else planned.slug) or planned.slug,
            description=meta.description if meta else None,
        ))
        for member in planned.bindings:
            disk_hash = aggregate_hash({
                e.path: e.content
                for e in list_content_files(member.root / member.rel_path)
                if e.in_git
            })
            self.db.upsert_binding(RuntimeBinding(
                id=f"{record.id}:{member.runtime_key}", skill_id=record.id,
                runtime_id=member.runtime_key, status="unmanaged",
                installed_commit=None, adapter_version=None,
                baseline_source="imported", installed_hash=disk_hash,
                installed_at=datetime.now(UTC),
            ))
        # 纳管不写 sync_records：溯源走 git log（FR-1.0.22 数据源直接用 git），
        # 且 ck_sync_action 白名单（push/pull/rollback/upgrade/resolve/defer）不含 import

    # ------------------------------------------------------------ status

    def _head(self) -> str:
        return self.git.resolve_ref("HEAD")

    def head(self) -> str:
        """当前 canonical HEAD（对外只读口径，MCP 等入口用）。"""
        return self._head()

    def status_matrix(self) -> list[DriftReport]:
        """四端漂移矩阵（FR-1.0.6）。「不适用」格子不进矩阵（04 §2.1.3）。"""
        out: list[DriftReport] = []
        adapters = {a.key: a for a in all_adapters()}
        for row in self.db.list_skills(SkillQuery(limit=10000)):
            try:
                snapshot = self.git.snapshot_at(self._head(), row.slug)
            except Exception:
                continue  # skill 行存在但 canonical 缺失：防御，doctor 会报
            for key, adapter in adapters.items():
                binding = self.db.get_binding(row.id, key)
                target = adapter.resolve_root("user", home=self.home) / row.slug
                disk_exists = target.exists()
                if not disk_exists and binding is None:
                    continue  # 该 runtime 上不适用
                out.append(self._one_cell(
                    row.id, key, adapter, snapshot, binding, target, disk_exists,
                ))
        return out

    def _one_cell(
        self, skill_id, key, adapter, snapshot, binding, target, disk_exists,
    ) -> DriftReport:
        desired = dict(adapter.render_layout(snapshot))
        remote_hash = aggregate_hash(desired)
        entries = list_content_files(target) if disk_exists else []
        local_hash = aggregate_hash({e.path: e.content for e in entries if e.in_git})
        has_untracked = any(not e.in_git for e in entries)

        if disk_exists and binding is None:
            state: DriftState = DriftState.UNMANAGED
            adapter_outdated = False
        elif not disk_exists and binding is not None:
            state = DriftState.MISSING
            adapter_outdated = False
        elif binding is not None and binding.baseline_source == "distributed":
            base_snapshot = self.git.snapshot_at(binding.installed_commit, snapshot.slug)
            base_hash = aggregate_hash(dict(adapter.render_layout(base_snapshot)))
            state, adapter_outdated = classify(
                adapter_new=(binding.adapter_version != adapter.version),
                disk_dirty=(local_hash != base_hash),
                content_new=(remote_hash != base_hash),
            )
        else:
            state = classify_imported(local_hash, remote_hash)
            adapter_outdated = False
        return DriftReport(
            skill_id=skill_id, runtime_key=key, state=state,
            base_commit=binding.installed_commit if binding else None,
            base_adapter_version=binding.adapter_version if binding else None,
            base_hash=binding.installed_hash if binding else None,
            local_hash=local_hash, remote_hash=remote_hash,
            adapter_outdated=adapter_outdated, has_untracked_assets=has_untracked,
        )

    def aggregate_status(self) -> dict[str, DriftState]:
        """按 Skill 聚合取最严重（FR-2.1.9）。"""
        worst: dict[str, DriftState] = {}
        for r in self.status_matrix():
            cur = worst.get(r.skill_id)
            if cur is None or _SEVERITY[r.state] > _SEVERITY[cur]:
                worst[r.skill_id] = r.state
        return worst

    # ------------------------------------------------------------ push / gate

    def _all_slugs(self) -> frozenset[str]:
        return frozenset(r.slug for r in self.db.list_skills(SkillQuery(limit=10000)))

    def gate_of(self, slug: str) -> GateResult:
        """质量门禁（唯一判定入口，check 与 push 共用 —— NFR-10.5）。"""
        snapshot = self.git.snapshot_at(self._head(), slug)
        return run_gate(snapshot, GateContext(known_slugs=self._all_slugs()))

    def push_skill(
        self,
        slug: str,
        *,
        runtime_keys: list[str] | None = None,
        dry_run: bool = False,
        allow_gate_failure: bool = False,
        confirmed_removals: frozenset[str] | None = None,
    ) -> DistributeReport:
        """一键分发（04 §5.2 时序：门禁 → 跨目标前置全检 → 逐目标事务 → 落库）。"""
        row = self.db.get_skill_by_slug(slug)
        if row is None:
            raise ValueError(f"未纳管的 skill: {slug}（先 skillos import）")
        with write_lock(self.paths.lock_dir):
            head = self._head()
            snapshot = self.git.snapshot_at(head, slug)
            # 步骤 3：门禁（error 级拦截；--allow-gate-failure 只在 CLI，MCP 无此开关）
            gate = run_gate(snapshot, GateContext(known_slugs=self._all_slugs()))
            if gate.has_error and not allow_gate_failure:
                return DistributeReport(
                    ok=False,
                    outcomes=[TargetOutcome(
                        runtime_key="*", ok=False, wrote=False, error_code="GATE_FAILED",
                        detail="; ".join(
                            f"{f.rule}: {f.message}"
                            for f in gate.findings if f.level == "error"
                        ),
                    )],
                )
            adapters = {a.key: a for a in all_adapters()}
            targets: list[TargetRequest] = []
            for key, adapter in adapters.items():
                if runtime_keys is not None and key not in runtime_keys:
                    continue
                binding = self.db.get_binding(row.id, key)
                deploy = adapter.resolve_root("user", home=self.home)
                # push 前残留定向检查（FR-1.0.15）
                residue = check_residue(deploy / slug)
                if residue.form != 0:
                    recover_residue(residue)
                targets.append(TargetRequest(
                    adapter=adapter, deploy_root=deploy,
                    facts=BindingFacts(
                        installed_hash=binding.installed_hash if binding else None,
                        baseline_source=binding.baseline_source if binding else None,
                    ),
                ))
            if not targets:
                raise ValueError("没有可用的分发目标（runtime_keys 过滤为空）")
            result = distribute_skill(
                snapshot, head, targets,
                policy=ReplacePolicy.RECORDED,
                backups_dir=self.paths.backups_dir,
                confirmed_removals=confirmed_removals,
                dry_run=dry_run,
            )
            if dry_run:
                return result
            # 步骤 8/9：基线落库 + 审计（首 push 成功即转 distributed）
            for outcome in result.outcomes:
                if not outcome.ok or outcome.new_installed_hash is None:
                    continue
                adapter = adapters[outcome.runtime_key]
                binding = self.db.get_binding(row.id, outcome.runtime_key)
                if binding is None:
                    binding = RuntimeBinding(
                        id=f"{row.id}:{outcome.runtime_key}",
                        skill_id=row.id, runtime_id=outcome.runtime_key,
                    )
                binding.status = "synced"
                binding.installed_commit = head
                binding.adapter_version = adapter.version
                binding.installed_hash = outcome.new_installed_hash
                binding.baseline_source = "distributed"
                binding.last_synced_at = datetime.now(UTC)
                self.db.upsert_binding(binding)
                self.db.record_sync(
                    skill_id=row.id, runtime_id=outcome.runtime_key, action="push",
                    from_state=None, to_state="synced",
                    detail={"skipped_assets": outcome.skipped_assets}, by="cli",
                )
            return result

    # ------------------------------------------------ 编辑写链路（P3，FR-2.4.5 / FR-3.13）

    def file_tree(self, slug: str) -> list[dict]:
        """canonical 文件树索引（云端只存树与哈希，不缓存内容 —— FR-3.12）。"""
        snapshot = self.git.snapshot_at(self._head(), slug)
        return [
            {
                "path": e.path,
                "hash": hashlib.sha256(e.content).hexdigest(),
                "size": len(e.content),
                "in_git": e.in_git,
            }
            for e in snapshot.files
        ]

    def write_file(
        self, slug: str, rel_path: str, content: bytes, *,
        base_hash: str, message: str, author: str = "web <web@skillos.local>",
    ) -> dict:
        """编辑器保存（FR-2.4.5 乐观锁）：base_hash 不匹配 → StaleWriteError（409 STALE）。

        写 canonical 工作树 + git commit；提交后跑门禁把逐条 findings 一并返回
        （保存不拦截——门禁拦 push，编辑期只提示，FR-1.0.11）。
        """
        row = self.db.get_skill_by_slug(slug)
        if row is None:
            raise ValueError(f"未纳管的 skill: {slug}")
        with write_lock(self.paths.lock_dir):
            head = self._head()
            snapshot = self.git.snapshot_at(head, slug)
            current = next((f for f in snapshot.files if f.path == rel_path), None)
            current_hash = (
                hashlib.sha256(current.content).hexdigest() if current is not None else ""
            )
            if current_hash != base_hash:
                raise StaleWriteError(
                    f"base_hash 不匹配（当前 {current_hash[:12]}… ≠ 提交的 "
                    f"{base_hash[:12]}…），请重新加载后再编辑（FR-3.13）"
                )
            target = safe_path(self.paths.repo_root / "skills" / slug, rel_path)
            if current is not None and current_hash == hashlib.sha256(content).hexdigest():
                # 幂等保存：内容未变（编辑器重复保存），不产生空 commit
                gate = run_gate(snapshot, GateContext(known_slugs=self._all_slugs()))
                return {
                    "commit": head,
                    "hash": current_hash,
                    "gate": [
                        {"rule": f.rule, "level": str(f.level), "message": f.message}
                        for f in gate.findings
                    ],
                    "gate_has_error": gate.has_error,
                }
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            commit = self.git.commit([f"skills/{slug}/{rel_path}"], message, author=author)
            gate = run_gate(
                self.git.snapshot_at(self._head(), slug),
                GateContext(known_slugs=self._all_slugs()),
            )
            return {
                "commit": commit,
                "hash": hashlib.sha256(content).hexdigest(),
                "gate": [
                    {"rule": f.rule, "level": str(f.level), "message": f.message}
                    for f in gate.findings
                ],
                "gate_has_error": gate.has_error,
            }

    # ------------------------------------------------ CRUD：新建（P3，FR-2.1.7）

    def create_skill(self, slug: str, *, name: str = "", description: str,
                     author: str = "web <web@skillos.local>") -> dict:
        """新建 Skill：canonical 模板落盘 + git commit + 索引（FR-2.1.7）。

        - slug 必须 kebab-case（与 fm.name.kebab 同口径）；占用判定走 path_key
          归一化（NFR-7.4：Demo/demo 归一化同键，拒绝而非静默并存）。
        - description 必填：fm.description.required 是 error 级门禁，
          创建即违规的 Skill 没有意义（写操作不猜用户意图，红线 16 同源）。
        - canonical 目录已存在而索引没有 → 报错不猜（磁盘与索引不一致）。
        - 不建 binding：未分发状态，首次 push 时建立（04 §2.2.5）。
        - 溯源走 git log（FR-1.0.22）；不写 sync_records（action 白名单无 create）。
        """
        if not is_kebab(slug):
            raise SkillValidationError(
                "SLUG_INVALID", f"slug 必须是 kebab-case（小写字母数字 + 连字符）: {slug!r}"
            )
        if not description.strip():
            raise SkillValidationError(
                "VALIDATION", "description 必填（fm.description.required 为 error 级门禁）"
            )
        display_name = name.strip() or slug
        with write_lock(self.paths.lock_dir):
            existing = {
                path_key(r.slug)
                for r in self.db.list_skills(SkillQuery(limit=10000))
            }
            if path_key(slug) in existing:
                raise SkillValidationError(
                    "SLUG_EXISTS", f"slug 已存在（含大小写归一化比对）: {slug}"
                )
            dest = self.paths.repo_root / "skills" / slug / "SKILL.md"
            if dest.parent.exists():
                # 索引里没有但磁盘有：重建守卫同源语义——判断不了就报错，不许猜
                raise SkillValidationError(
                    "SLUG_EXISTS", f"canonical 目录已存在但未纳管: skills/{slug}"
                )
            content = _render_skill_md(display_name, description)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
            commit = self.git.commit(
                [f"skills/{slug}"], f"skillos: create {slug}", author=author
            )
            self.db.upsert_skill(SkillRecord(
                id="", slug=slug, name=display_name, description=description,
            ))
            gate = run_gate(
                self.git.snapshot_at(self._head(), slug),
                GateContext(known_slugs=self._all_slugs()),
            )
            return {
                "slug": slug,
                "commit": commit,
                "hash": hashlib.sha256(content).hexdigest(),
                "gate": [
                    {"rule": f.rule, "level": str(f.level), "message": f.message}
                    for f in gate.findings
                ],
                "gate_has_error": gate.has_error,
            }

    def delete_skill(self, slug: str, *, dry_run: bool = True,
                     confirmed_removals: list[str] | None = None,
                     author: str = "web <web@skillos.local>") -> dict:
        """删除 Skill（P3 CRUD）。只删 canonical + 索引；**runtime 磁盘绝不静默删**
        （红线 16：处置权在用户，删除后 runtime 目录保留，可 re-import 或手动清理）。

        安全语义（与 rollback 同判据模式，FR-1.3.7）：
        - FR-1.0.26 ②：删除会移除 canonical 全部纳管文件 → dry_run 先列出
          将消失的路径（比较对象：canonical HEAD 快照），未逐条确认前整体拒绝。
        - 红线 16 变体：canonical 目录出现 HEAD 之外的文件（外部手动放入）→
          RESIDUE 报错列出差异，**绝不静默连它一起删**；判断不了就报错，不许猜。
        - 溯源：delete commit 进 git log（FR-1.0.22），历史仍可查；
          不写 sync_records（action 白名单无 delete）。
        """
        row = self.db.get_skill_by_slug(slug)
        if row is None:
            raise SkillValidationError("NOT_FOUND", f"未纳管的 skill: {slug}")
        dest = self.paths.repo_root / "skills" / slug

        def _plan() -> dict:
            """同判据核心：HEAD 纳管文件集 vs canonical 磁盘文件集。"""
            head_map = {
                f.path for f in self.git.snapshot_at(self._head(), slug).files if f.in_git
            }
            disk = {e.path for e in list_content_files(dest)} if dest.is_dir() else set()
            return {
                "removals": sorted(head_map),
                "stray": sorted(disk - head_map),
                "missing": sorted(p for p in head_map if p not in disk),
            }

        plan = _plan()
        if plan["stray"] or plan["missing"]:
            detail = (
                f"canonical 目录存在 HEAD 之外的文件: {plan['stray']}"
                if plan["stray"]
                else f"HEAD 中列出的文件在磁盘上缺失: {plan['missing']}"
            )
            return {
                "ok": False, "error_code": "RESIDUE",
                "detail": f"canonical 与 HEAD 不一致，须先处理后再删除（{detail}）",
                "removals": plan["stray"] or plan["missing"],
            }
        if not plan["removals"]:
            # 索引说有、HEAD 说无：不一致，报错不猜（同 rebuild 守卫语义）
            return {
                "ok": False, "error_code": "RESIDUE",
                "detail": "canonical HEAD 下无该 skill 的文件（索引与 git 不一致）",
                "removals": [],
            }
        if dry_run:
            return {"ok": True, "dry_run": True, "removals": plan["removals"]}

        with write_lock(self.paths.lock_dir):
            plan = _plan()  # 锁内重算（FR-1.3.7：预览即真跑会遇到的一切）
            if plan["stray"] or plan["missing"] or not plan["removals"]:
                return {
                    "ok": False, "error_code": "RESIDUE",
                    "detail": "预览与真跑之间 canonical 发生变化，请重试 dry_run",
                    "removals": plan["stray"] or plan["missing"],
                }
            removals = plan["removals"]
            confirmed = set(confirmed_removals or [])
            if set(removals) != confirmed:
                return {
                    "ok": False, "error_code": "REMOVALS_UNCONFIRMED",
                    "detail": "删除将使下列文件从 canonical 消失，须在 Web 逐条确认后重试",
                    "removals": [p for p in removals if p not in confirmed],
                }
            # 只删已确认的文件；删除失败（并发消失等）→ 报错，绝不部分成功
            for rel in removals:
                p = safe_path(dest, rel)
                if not p.is_file():
                    return {
                        "ok": False, "error_code": "RESIDUE",
                        "detail": f"待删文件在执行前消失: {rel}",
                        "removals": [rel],
                    }
                p.unlink()
            # 空目录清理：只 rmdir 空目录，非空跳过（不删任何未确认内容）
            for sub in sorted(dest.rglob("*"), key=lambda x: len(x.parts), reverse=True):
                if sub.is_dir():
                    try:
                        sub.rmdir()
                    except OSError:
                        pass
            try:
                dest.rmdir()
            except OSError:
                pass
            commit = self.git.commit(
                [f"skills/{slug}"], f"skillos: delete {slug}", author=author
            )
            self.db.delete_bindings(row.id)
            self.db.delete_skill(row.id)
            return {"ok": True, "commit": commit, "removals": removals}

    def sandbox_reset(self) -> dict:
        """恢复预置演示数据（FR-3.10 / P7）。仅沙盒模式可达。

        语义：把容器内演示环境恢复到 seed 状态 —— runtime deploy 根清空后从
        seed 复制、canonical 重建、索引重导并 push 全部（矩阵归 SYNCED）。

        安全约束：
        - 非沙盒模式拒绝（NOT_SANDBOX）；seed 缺失拒绝（SEED_MISSING）——
          reset ≠ 清空，没有可恢复的预置就不执行。
        - **runtime root 必须在容器根（home）之内**（BOUNDARY）——越界一律
          报错，绝不删任何内容（红线 16：判断不了就报错，不许猜）。
        - 锁边界（T17）：文件恢复 + 索引清空在写锁内；import/push 各自
          持锁，在本方法锁外顺序调用（同进程双 FileLock 会自锁，实测踩坑）。
        - reset commit 即溯源（FR-1.0.22）：重置本身也写进 git log。
        """
        if not self.is_sandbox:
            raise SkillValidationError(
                "NOT_SANDBOX", "仅沙盒模式（SKILLOS_SANDBOX=1）可重置"
            )
        seed = self.home / ".skillos-seed" / "runtime"
        if not seed.is_dir():
            raise SkillValidationError(
                "SEED_MISSING", f"预置数据目录不存在: {seed}（reset ≠ 清空，拒绝执行）"
            )
        home_root = self.home.resolve()
        runtimes = self.db.list_runtimes()
        # 边界守卫：先全量校验，再动任何文件（部分校验后部分删除 = 半状态灾难）
        for rt in runtimes:
            root = Path(rt.root_path).resolve()
            if home_root not in root.parents:
                raise SkillValidationError(
                    "BOUNDARY",
                    f"runtime root 越出容器根，拒绝重置: {rt.key} -> {root}",
                )

        with write_lock(self.paths.lock_dir):
            # 1. runtime：deploy 根清空，从 seed/<key>/ 复制
            for rt in runtimes:
                root = Path(rt.root_path)
                root.mkdir(parents=True, exist_ok=True)
                for child in root.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                src = seed / rt.key
                if src.is_dir():
                    shutil.copytree(src, root, dirs_exist_ok=True)
            # 2. canonical：整目录重建（含 .git，回到出厂）。git objects 在
            # Windows 下是只读文件（git 默认属性），rmtree 需先 chmod 再删；
            # rmtree 后目录不存在，git subprocess 以 repo 为 cwd，必须先建
            if self.paths.repo_root.exists():
                shutil.rmtree(self.paths.repo_root, onerror=_rm_readonly)
            self.paths.repo_root.mkdir(parents=True, exist_ok=True)
            self.git.init()
            # 3. 索引：skills / bindings 清空（runtimes 表保留，root 不变）
            for row in self.db.list_skills(SkillQuery(limit=10000)):
                self.db.delete_bindings(row.id)
                self.db.delete_skill(row.id)

        # 4. 锁外（各方法自持锁）：重导 + push 全部
        self.import_skills()
        restored = []
        for r in self.db.list_skills(SkillQuery(limit=10000)):
            report = self.push_skill(r.slug)
            if not report.ok:
                raise RuntimeError(
                    f"沙盒重置后 push 失败: {r.slug} "
                    f"{[(o.error_code, o.detail) for o in report.outcomes]}"
                )
            restored.append(r.slug)
        commit = self._head()
        return {"restored": True, "skills": sorted(restored), "commit": commit}

    # ------------------------------------------------ 版本管理（P4，FR-2.2.8 / 1.0.22 / 1.0.26）

    def rollback_skill(self, slug: str, ref: str, *,
                       dry_run: bool = True,
                       confirmed_removals: list[str] | None = None,
                       author: str = "web <web@skillos.local>") -> dict:
        """回滚 canonical 到指定版本（FR-2.2.8）。

        安全语义（三条红线一个不缺）：
        - FR-1.0.10：git.rollback 内部先自动建安全快照（含 dirty），快照可见、可再回滚。
        - FR-1.0.26 ②：回滚会删内容（新版本有而旧版本无的文件）→ dry_run 先列出
          将消失的路径，**未逐条确认前整体拒绝**（回滚要的是完整一致状态，
          不像 push 有 held_back 的部分语义）。
        - FR-1.0.22：回滚 commit 即溯源（author + 目标版本入 git 历史，不可篡改）；
          另落 sync_records（NFR-9：每次回滚均写操作账本）。
        - FR-1.3.7：dry_run 与真跑共用 `_rb_plan` 同一判据，锁内重算。
        """
        row = self.db.get_skill_by_slug(slug)
        if row is None:
            raise ValueError(f"未纳管的 skill: {slug}")
        try:
            target = self.git.resolve_ref(ref)
        except GitError as e:
            raise ValueError(f"无法解析目标版本 {ref}: {e}") from e

        def _rb_plan() -> dict:
            """同判据核心：head vs target 的文件集差。"""
            head_map = {f.path: f.content for f in
                        self.git.snapshot_at(self._head(), slug).files if f.in_git}
            tgt_map = {f.path: f.content for f in
                       self.git.snapshot_at(target, slug).files if f.in_git}
            return {
                "removals": sorted(p for p in head_map if p not in tgt_map),
                "changed": sorted(p for p in tgt_map
                                  if p in head_map and head_map[p] != tgt_map[p]),
                "restored": sorted(p for p in tgt_map if p not in head_map),
            }

        plan = _rb_plan()
        if not (plan["removals"] or plan["changed"] or plan["restored"]):
            return {"ok": True, "noop": True, "removals": [], "changed": [], "restored": []}
        preview = {"ok": True, "dry_run": True, "target": target, **plan}
        if dry_run:
            return preview

        with write_lock(self.paths.lock_dir):
            # 锁内重算（FR-1.3.7：预览即真跑会遇到的一切）
            plan = _rb_plan()
            removals = plan["removals"]
            confirmed = set(confirmed_removals or [])
            if removals and set(removals) != confirmed:
                missing = [p for p in removals if p not in confirmed]
                return {
                    "ok": False, "error_code": "REMOVALS_UNCONFIRMED",
                    "detail": "回滚将使下列文件消失，须在 Web 逐条确认后重试",
                    "removals": missing,
                }
            if not (plan["removals"] or plan["changed"] or plan["restored"]):
                return {"ok": True, "noop": True,
                        "removals": [], "changed": [], "restored": []}
            commit = self.git.rollback(target, [f"skills/{slug}"], author=author)
            # NFR-9：回滚进操作账本（action 白名单 ck_sync_action 含 rollback）
            self.db.record_sync(
                skill_id=row.id, runtime_id=None, action="rollback",
                from_state=None, to_state=None,
                detail={"target": target, **plan}, by=author,
            )
            return {"ok": True, "commit": commit, "target": target, **plan}

    def file_history(self, slug: str, rel_path: str, limit: int = 50) -> list[dict]:
        """文件级历史（P4）：--follow 跨改名追踪该文件的提交序列。"""
        row = self.db.get_skill_by_slug(slug)
        if row is None:
            raise ValueError(f"未纳管的 skill: {slug}")
        commits = self.git.log(
            path=f"skills/{slug}/{rel_path}", limit=limit, follow=True,
        )
        return [{"sha": c.sha, "author": c.author, "at": c.at, "message": c.message}
                for c in commits]

    def list_tags(self) -> list[dict]:
        """tag 清单（时间轴徽标数据源）。"""
        return self.git.list_tags()

    def create_tag(self, name: str, message: str = "",
                   author: str = "web <web@skillos.local>") -> dict:
        """在当前 HEAD 打附注 tag（P4）。tag 本身即 git 溯源，不另落库。"""
        if not name or "/" in name or name.startswith("-"):
            raise ValueError(f"非法 tag 名: {name}")
        self.git.create_tag(name, message or f"skillos: tag {name}")
        return {"ok": True, "name": name}

    # ------------------------------------------------ 回收（P5，FR-2.2.6 / FR-1.3.7）
    def pull_skill(self, slug: str, runtime_key: str, *, dry_run: bool = False) -> dict:
        """从 Runtime 磁盘回收改动为 canonical 新版本（FR-2.2.6）。

        第一版边界（危险决策留给人工，FR-1.3.5）：
        - 仅当 canonical 自 binding 基线后**未前进**才回收；双向分歧 → CONFLICT。
        - dry_run 与真跑共用 `_pull_diff` 同一判据（FR-1.3.7 / 07 N17）。
        - 冲突不提供 on_conflict / force，交 Web 人工路径。
        """
        row = self.db.get_skill_by_slug(slug)
        if row is None:
            raise ValueError(f"未纳管的 skill: {slug}")
        adapters = {a.key: a for a in all_adapters()}
        if runtime_key not in adapters:
            raise ValueError(f"未知 runtime: {runtime_key}")
        binding = self.db.get_binding(row.id, runtime_key)
        if binding is None or binding.installed_commit is None:
            raise ValueError(f"{slug} 在 {runtime_key} 无纳管基线，无改动可回收")
        deploy = adapters[runtime_key].resolve_root("user", home=self.home) / slug

        def _pull_diff() -> tuple[dict, dict, dict, dict]:
            """同判据核心：磁盘内容 vs canonical@installed_commit。
            dry_run（锁外）与真跑（锁内）都调用它 —— 预览即真跑会遇到的一切。"""
            disk = {e.path: e.content for e in list_content_files(deploy) if e.in_git}
            base = self.git.snapshot_at(binding.installed_commit, slug)  # type: ignore[arg-type]
            base_map = {f.path: f.content for f in base.files if f.in_git}
            changed = {p: c for p, c in disk.items() if p in base_map and base_map[p] != c}
            added = {p: c for p, c in disk.items() if p not in base_map}
            removed = [p for p in base_map if p not in disk]
            return disk, changed, added, removed

        # canonical 是否已前进（基线之后有该 skill 的提交）
        head = self._head()
        base_snap = self.git.snapshot_at(binding.installed_commit, slug)  # type: ignore[arg-type]
        head_snap = self.git.snapshot_at(head, slug)
        base_map = {f.path: f.content for f in base_snap.files if f.in_git}
        head_map = {f.path: f.content for f in head_snap.files if f.in_git}
        if head_map != base_map:
            return {
                "ok": False, "error_code": "CONFLICT",
                "detail": "canonical 在基线之后已有新提交（双向分歧），回收需人工在 Web 处理",
            }

        _disk, changed, added, removed = _pull_diff()
        if not (changed or added or removed):
            return {"ok": True, "noop": True, "changed": [], "added": [], "removed": []}
        preview = {
            "ok": True, "dry_run": True,
            "changed": sorted(changed), "added": sorted(added),
            "removed": removed,
        }
        if dry_run:
            return preview

        with write_lock(self.paths.lock_dir):
            # 锁内重算（FR-1.3.7：判据与预览完全一致）
            disk, changed, added, removed = _pull_diff()
            if not (changed or added or removed):
                return {"ok": True, "noop": True, "changed": [], "added": [], "removed": []}
            skill_dir = self.paths.repo_root / "skills" / slug
            for rel in list(changed) + list(added):
                target = safe_path(skill_dir, rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(disk[rel])
            for rel in removed:
                safe_path(skill_dir, rel).unlink(missing_ok=True)
            commit = self.git.commit(
                [f"skills/{slug}"], f"pull: 回收 {runtime_key} 磁盘改动为 canonical 新版本",
            )
            binding.status = "synced"
            binding.installed_commit = commit
            binding.baseline_source = "imported"  # 磁盘内容成为新基线（04 §2.2.5 同源语义）
            binding.installed_hash = aggregate_hash(
                {p: c for p, c in disk.items()}
            )
            binding.last_synced_at = datetime.now(UTC)
            self.db.upsert_binding(binding)
            self.db.record_sync(
                skill_id=row.id, runtime_id=runtime_key, action="pull",
                from_state="modified", to_state="synced",
                detail={"changed": sorted(changed), "added": sorted(added),
                        "removed": removed},
                by="mcp",
            )
            return {
                "ok": True, "commit": commit,
                "changed": sorted(changed), "added": sorted(added),
                "removed": removed,
            }

    # ------------------------------------------------ CONFLICT 四选一（P3，FR-2.2.6 / T22 / RED LINE 12）
    def resolve_conflict(
        self,
        slug: str,
        runtime_key: str,
        decision: str,
        *,
        confirmed_removals: list[str] | None = None,
        author: str = "web <web@skillos.local>",
    ) -> dict:
        """CONFLICT 决策（FR-2.2.6 / RED LINE 12）。

        整文件粒度四选一，前置自动安全快照（FR-1.0.10 同源能力）：
        - keep_local  保留本地：磁盘 → canonical 新版本（同 pull_skill 路径）
        - keep_remote 保留仓库：USER_CONFIRMED policy 写磁盘
        - keep_both   两个都保留（Web 专属）：磁盘 → *.local + canonical → 磁盘
        - defer       暂不处理：不改任何内容，仅落 sync_records

        安全线（RED LINE 12）：
        - MCP / CLI / daemon 自动路径不得暴露 keep_both —— 此方法保留 keep_both 接口
          （Web 路由可调），MCP 工具签名里没有该参数（test_conflicts_p3 红线守护）。
        - 决策前如 canonical 工作树 dirty，先 git commit 一次（决策回滚的安全锚点）。
        - 不做块内合并；冲突整文件取舍。

        返回：{"ok", "noop"?, "decision", "commit"?, "snapshot"?, "by"?, "error"?, ...}
        异常：未纳管 / 未知 runtime / 非法决策 / 非 CONFLICT 状态 → ValueError。
        """
        if decision not in _VALID_DECISIONS:
            raise ValueError(
                f"非法决策: {decision!r}（仅 {_VALID_DECISIONS}）"
            )
        row = self.db.get_skill_by_slug(slug)
        if row is None:
            raise ValueError(f"未纳管的 skill: {slug}")
        adapters = {a.key: a for a in all_adapters()}
        if runtime_key not in adapters:
            raise ValueError(f"未知 runtime: {runtime_key}")
        binding = self.db.get_binding(row.id, runtime_key)
        if binding is None:
            raise ValueError(
                f"{slug} 在 {runtime_key} 无 binding（UNMANAGED），先 import/push"
            )

        deploy = adapters[runtime_key].resolve_root("user", home=self.home) / slug

        with write_lock(self.paths.lock_dir):
            # 锁内重算冲突状态（FR-1.3.7：与预览同判据）
            head = self._head()
            head_snap = self.git.snapshot_at(head, slug)
            adapter = adapters[runtime_key]
            disk_managed = {
                e.path: e.content for e in list_content_files(deploy) if e.in_git
            }
            head_map = {f.path: f.content for f in head_snap.files if f.in_git}
            base_snap = self.git.snapshot_at(binding.installed_commit, slug)
            base_map = {f.path: f.content for f in base_snap.files if f.in_git}

            # CONFLICT 真身 = disk 改变 + canonical 前进。比较走 aggregate_hash：
            # disk 已 render_layout() 转译，与 base_snap raw 字节不等属正常 —— 仅当
            # 整体哈希改变才算"用户改了"。这与 drift.classify() 同源（FR-1.0.6）。
            disk_hash = aggregate_hash(disk_managed)
            head_hash = aggregate_hash(head_map)
            base_hash = binding.installed_hash  # 落库锚点 = render_layout 的字节哈希
            disk_changed = (disk_hash != base_hash)
            canonical_advanced = (head_hash != base_hash)
            if not (disk_changed and canonical_advanced):
                raise ValueError(
                    "非 CONFLICT 状态（disk 或 canonical 仅单侧改变，走 OUTDATED / MODIFIED，"
                    "由 push / pull 自然处理；本方法只接 CONFLICT）"
                )

            # 步骤 0：决策前快照（FR-1.0.10 / T22）。
            # 仅当 canonical 工作树 dirty 时落一个空 commit；否则保留 head 不动。
            snapshot_sha = self._snapshot_dirty(f"resolve: snapshot before {decision}")

            # 步骤 1：按决策实施
            if decision == ConflictDecision.DEFER:
                # 暂不处理：不动任何内容，只落 sync_records
                self.db.record_sync(
                    skill_id=row.id, runtime_id=runtime_key, action="resolve",
                    from_state="conflict", to_state="conflict_deferred",
                    detail={"decision": decision, "snapshot": snapshot_sha},
                    by=author,
                )
                return {
                    "ok": True, "noop": True, "decision": decision,
                    "snapshot": snapshot_sha, "by": author,
                }

            if decision == ConflictDecision.KEEP_LOCAL:
                # 保留本地：把磁盘纳管内容回收为 canonical 新版本
                changed, added, removed = self._pull_apply(
                    slug, runtime_key, deploy, disk_managed, base_map,
                )
                commit = self.git.commit(
                    [f"skills/{slug}"],
                    f"resolve: keep_local from {runtime_key}",
                    author=author,
                )
                self._mark_synced(binding, runtime_key, adapter, commit, disk_managed)
                self.db.record_sync(
                    skill_id=row.id, runtime_id=runtime_key, action="resolve",
                    from_state="conflict", to_state="synced",
                    detail={
                        "decision": decision,
                        "snapshot": snapshot_sha,
                        "commit": commit,
                        "changed": sorted(changed), "added": sorted(added),
                        "removed": removed,
                    },
                    by=author,
                )
                return {
                    "ok": True, "decision": decision, "commit": commit,
                    "snapshot": snapshot_sha, "by": author,
                    "changed": sorted(changed), "added": sorted(added),
                    "removed": removed,
                }

            if decision == ConflictDecision.KEEP_REMOTE:
                # 保留仓库：USER_CONFIRMED 覆盖磁盘
                self._distribute_one(
                    head_snap, adapter, deploy, binding,
                    confirmed=confirmed_removals,
                )
                self._mark_synced(
                    binding, runtime_key, adapter, head,
                    {f.path: f.content for f in head_snap.files if f.in_git},
                )
                self.db.record_sync(
                    skill_id=row.id, runtime_id=runtime_key, action="resolve",
                    from_state="conflict", to_state="synced",
                    detail={
                        "decision": decision,
                        "snapshot": snapshot_sha,
                        "kept_remote": True,
                    },
                    by=author,
                )
                return {
                    "ok": True, "decision": decision,
                    "snapshot": snapshot_sha, "by": author,
                }

            # KEEP_BOTH：Web 专属。MCP / daemon 自动路径不得触发（FR-1.3.5 / RED LINE 12）。
            # 手工落盘不走 distribute —— 避免「*.local-conflict.md 触发 removals 守卫
            # 必须 confirm 删除」的悖论（设计要求"两边都进库"）：
            # 1) 磁盘纳管文件 → <basename>.local-conflict<ext>（保留本地副本）
            # 2) canonical@HEAD 内容写入原 basename（覆盖为仓库版本）
            # 3) 标记 binding 为 synced（installed_hash 用 canonical 内容）
            head_disk_map = {f.path: f.content for f in head_snap.files if f.in_git}
            self._keep_both_rename(disk_managed, deploy)
            self._write_canonical_to_disk(deploy, head_disk_map)
            self._mark_synced(
                binding, runtime_key, adapter, head, head_disk_map,
            )
            self.db.record_sync(
                skill_id=row.id, runtime_id=runtime_key, action="resolve",
                from_state="conflict", to_state="synced",
                detail={
                    "decision": decision,
                    "snapshot": snapshot_sha,
                    "kept_both": sorted(disk_managed.keys()),
                },
                by=author,
            )
            return {
                "ok": True, "decision": decision,
                "snapshot": snapshot_sha, "by": author,
                "kept_local_files": sorted(
                    rel + ".local-conflict" + Path(rel).suffix
                    for rel in disk_managed
                ),
            }

    # ------- resolve_conflict 内部辅助（同文件方法便于测试访问）

    def _snapshot_dirty(self, message: str) -> str | None:
        """决策前自动快照：工作树 dirty 时落一个空 commit，否则返回当前 head。"""
        # dirty 判定：status --porcelain 非空
        status = self.git._git("status", "--porcelain", "--", "skills/").stdout.decode(
            "utf-8", errors="replace"
        ).strip()
        if not status:
            return self._head()
        # 空提交（决策前携带决策意图，将来可一键 revert）
        return self.git.commit(["skills/"], message)

    def _pull_apply(
        self, slug: str, runtime_key: str, deploy: Path,
        disk_managed: dict[str, bytes], base_map: dict[str, bytes],
    ) -> tuple[list[str], list[str], list[str]]:
        """KEEP_LOCAL 实施：磁盘纳管内容覆盖到 canonical 工作树 + git commit。
        返回 (changed, added, removed) 路径列表。
        """
        skill_dir = self.paths.repo_root / "skills" / slug
        changed = []
        added = []
        for rel, content in disk_managed.items():
            target = safe_path(skill_dir, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            if rel in base_map:
                changed.append(rel)
            else:
                added.append(rel)
        for rel in base_map:
            if rel not in disk_managed:
                target = safe_path(skill_dir, rel)
                if target.exists():
                    target.unlink()
        removed = [r for r in base_map if r not in disk_managed]
        return changed, added, removed

    def _keep_both_rename(self, disk_managed: dict[str, bytes], deploy: Path) -> None:
        """KEEP_BOTH 第 1 步：磁盘纳管文件 → *.local-conflict<ext>（保留本地副本）。

        命名：把 `<basename><.suffix>` 拆开， `<basename>` + `.local-conflict` + 原后缀
        （设计文档约定：SKILL.md → SKILL.local-conflict.md，FR-2.2.6）。
        """
        for rel, content in disk_managed.items():
            p = Path(rel)
            conflict_rel = (
                f"{p.stem}.local-conflict{p.suffix}"
                if p.suffix else f"{rel}.local-conflict"
            )
            dst = safe_path(deploy, conflict_rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(content)

    def _write_canonical_to_disk(
        self, deploy: Path, head_disk_map: dict[str, bytes],
    ) -> None:
        """KEEP_BOTH 第 2 步：把 canonical@HEAD 内容写到磁盘原 basename 位置（覆盖）。"""
        for rel, content in head_disk_map.items():
            target = safe_path(deploy, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def _distribute_one(
        self,
        head_snap: SkillSnapshot,
        adapter: RuntimeAdapter,
        deploy: Path,
        binding: RuntimeBinding,
        *,
        confirmed: list[str] | None,
    ) -> None:
        """单目标 USER_CONFIRMED 写入（KEEP_REMOTE / KEEP_BOTH 共用）。"""
        targets = [TargetRequest(
            adapter=adapter, deploy_root=deploy.parent,
            facts=BindingFacts(
                installed_hash=binding.installed_hash,
                baseline_source=binding.baseline_source,
            ),
        )]
        report = distribute_skill(
            head_snap, self._head(), targets,
            policy=ReplacePolicy.USER_CONFIRMED,
            backups_dir=self.paths.backups_dir,
            confirmed_removals=frozenset(confirmed) if confirmed else None,
        )
        if not report.ok:
            err = report.error_code or next(
                (o.error_code for o in report.outcomes if not o.ok), "DISTRIBUTE_FAILED"
            )
            raise ValueError(f"覆盖失败（policy=USER_CONFIRMED）: {err}")

    def _mark_synced(
        self,
        binding: RuntimeBinding,
        runtime_key: str,
        adapter: RuntimeAdapter,
        commit: str,
        installed_disk: dict[str, bytes],
    ) -> None:
        """resolve_conflict 落库：binding 转 synced，installed_commit + installed_hash 更新。"""
        binding.status = "synced"
        binding.installed_commit = commit
        binding.adapter_version = adapter.version
        binding.installed_hash = aggregate_hash(installed_disk)
        binding.baseline_source = "distributed"
        binding.last_synced_at = datetime.now(UTC)
        self.db.upsert_binding(binding)

    # ------------------------------------------------ 折旧度量（P3，FR-1.0.23）
    def decay_metrics(self, slug: str) -> dict:
        """折旧两维度（纯静态分析，排期 P3）：staleness + dependency_broken。

        - staleness_days：距最后一次触碰该 skill 的 commit 的天数
        - dependency_broken：frontmatter `requires` 声明但库中不存在的依赖
          （依赖声明口径：meta.extra.requires，标准字段集未含依赖，暂以此为准）
        upstream_divergence 依赖 FR-1.0.13（P7），只留接口不入本期。
        """
        commits = self.git.log(path=f"skills/{slug}", limit=1)
        staleness_days: float | None = None
        if commits:
            last = datetime.fromisoformat(commits[0].at)
            staleness_days = round(
                (datetime.now(last.tzinfo) - last).total_seconds() / 86400, 1
            )
        snapshot = self.git.snapshot_at(self._head(), slug)
        declared = snapshot.meta.extra.get("requires") or []
        if not isinstance(declared, list):
            declared = [str(declared)]
        known = self._all_slugs()
        broken = [str(d) for d in declared if str(d) not in known]
        return {
            "slug": slug,
            "staleness_days": staleness_days,
            "dependency_broken": broken,
        }


def _rm_readonly(func, path: str, _exc) -> None:
    """rmtree 辅助：git objects 在 Windows 下为只读，先 chmod 再重删。

    用 onerror 签名（func, path, exc_info）——3.13 下 onexc 虽新，
    但运行时 shim 环境可能不支持，onerror 跨版本可用。
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


class StaleWriteError(Exception):
    """base_hash 乐观锁冲突（FR-3.13）：客户端必须重新加载，不允许覆盖。"""


class SkillValidationError(ValueError):
    """CRUD 入参校验失败（P3）。code：SLUG_INVALID / SLUG_EXISTS / VALIDATION。

    继承 ValueError：CLI 侧按现有 except ValueError 打印，API 侧按 code 映射 4xx。
    """

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _render_skill_md(name: str, description: str) -> bytes:
    """新建模板（FR-2.1.7）。name/description 用 JSON 双引号转义——
    JSON 字符串是合法 YAML 标量，中文 / 冒号 / 引号混排都安全（05 P0-3 编码）。"""
    fm_name = json.dumps(name, ensure_ascii=False)
    fm_desc = json.dumps(description, ensure_ascii=False)
    return (
        "---\n"
        f"name: {fm_name}\n"
        f"description: {fm_desc}\n"
        "---\n"
        f"\n# {name}\n\n在 Web 或编辑器中补充正文。\n"
    ).encode("utf-8")
