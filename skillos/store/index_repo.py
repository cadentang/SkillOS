"""索引读写（04 §2.3）。两个实现：SqliteIndexStore（本地）、PostgresIndexStore（云端）。

本地 SQLite 是缓存不是事实源（FR-1.0.30）：可随时从 git 仓库 + 磁盘重建。
检索第一版用 LIKE（FTS5 全文检索排期 P2 接 Web 搜索时补）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, aliased

from skillos.store.models import (
    AppSetting,
    HealthReport,
    Runtime,
    RuntimeBinding,
    Skill,
    SkillCheck,
    SkillDependency,
    SkillSimilarity,
    SkillTag,
    SkillUsageDaily,
    SkillUsageEvent,
    SyncRecord,
    UpstreamSource,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _ulid() -> str:
    return uuid.uuid4().hex


@dataclass
class UpstreamSourceRecord:
    """上游来源（FR-1.0.13 / E-08）：`upstream_sources` 是上游事实的唯一权威。

    `consecutive_failures` 是失败计数器而非状态：只有累计到阈值才把 status 推到
    `error`（E-08.4），单次网络抖动不该让健康度从 ok 掉到 degraded。
    """

    skill_id: str
    type: str = "local"          # git / marketplace / url / local
    url: str | None = None
    ref: str | None = None
    base_version: str | None = None
    upstream_hash: str | None = None
    last_checked_at: datetime | None = None
    status: str = "unknown"      # current / outdated / error / unknown
    consecutive_failures: int = 0


@dataclass(frozen=True)
class SkillQuery:
    q: str | None = None
    category: str | None = None
    tag: str | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True)
class SkillRecord:
    """services 层使用的领域记录（与 ORM 解耦）。"""

    id: str
    slug: str
    name: str
    description: str | None = None
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    # 相似对的推荐链要按"谁更早创建"裁决（N-07.2），读侧必须能拿到该列
    created_at: datetime | None = None
    # N-07.3 弃用软状态：保留记录与历史、bindings 已回收，重新 push 即恢复
    deprecated: bool = False


class IndexStore(Protocol):
    """数据访问协议（04 §2.3）。"""

    # skills
    def upsert_skill(self, skill: SkillRecord) -> SkillRecord: ...
    def get_skill_by_slug(self, slug: str) -> SkillRecord | None: ...
    def set_skill_deprecated(self, skill_id: str, deprecated: bool) -> bool: ...
    def list_deprecated_slugs(self) -> list[str]: ...
    def list_skills(self, q: SkillQuery) -> list[SkillRecord]: ...
    def delete_skill(self, skill_id: str) -> None: ...
    # 上游来源（FR-1.0.13 / E-08）：upstream_sources 是上游事实的唯一权威
    def get_upstream_source(self, skill_id: str) -> UpstreamSourceRecord | None: ...
    def list_upstream_sources(self) -> list[UpstreamSourceRecord]: ...
    def upsert_upstream_source(self, s: UpstreamSourceRecord) -> None: ...
    # runtimes
    def upsert_runtime(self, runtime_id: str, key: str, adapter_key: str, root_path: Path,
                       scope: str) -> None: ...
    def list_runtimes(self) -> list[Runtime]: ...
    def set_runtime_enabled(self, key: str, enabled: bool) -> bool: ...
    def delete_runtime(self, key: str) -> bool: ...
    # bindings
    def upsert_binding(self, b: RuntimeBinding) -> RuntimeBinding: ...
    def get_binding(self, skill_id: str, runtime_id: str) -> RuntimeBinding | None: ...
    def list_bindings(self, skill_id: str | None = None) -> list[RuntimeBinding]: ...
    def delete_bindings(self, skill_id: str) -> None: ...
    # 设备侧开关（V2-T1）：value 为 JSON 字符串
    def get_setting(self, key: str) -> str | None: ...
    def set_setting(self, key: str, value: str) -> None: ...
    # 调用遥测（N-11.1）：事件表是聚合的唯一事实源
    def add_usage_event(self, *, session_id: str, runtime: str, skill_id: str, op: str,
                        via: str, duration_ms: int | None, ok: bool,
                        param_digest: str | None,
                        tokens_in: int = 0, tokens_out: int = 0) -> None: ...
    def list_usage_events(self, since: datetime) -> list[SkillUsageEvent]: ...
    def replace_usage_daily(self, since: date, rows: list[dict]) -> int: ...
    def purge_usage_events(self, before: datetime) -> int: ...
    def list_usage_daily(self, since: date) -> list[SkillUsageDaily]: ...
    # 检查结果（E-02.6）：折旧维度一行一个，写入前先清该 skill 的旧 decay 行
    def replace_decay_checks(self, skill_id: str, rows: list[dict]) -> int: ...
    def list_checks(self, scope: str | None = None) -> list[SkillCheck]: ...
    # 体检报告（N-02.3）：本地表，daemon/CLI 生成，Web 经 daemon 读取
    def add_health_report(self, *, period_start: datetime, period_end: datetime,
                          payload: dict, unchanged: bool) -> str: ...
    def list_health_reports(self, limit: int = 20) -> list[HealthReport]: ...
    def get_health_report(self, report_id: str) -> HealthReport | None: ...
    # 单 skill 调用记录（N-11.7）：事件按时间倒序 + session 链路
    def list_skill_events(self, skill_id: str, limit: int = 100) -> list[SkillUsageEvent]: ...
    # 依赖图（E-02.1 反向入度）：(声明方 slug, 被依赖 slug)
    def list_dependency_edges(self) -> list[tuple[str, str]]: ...
    # sync 审计（NFR-9）
    def record_sync(self, skill_id: str | None, runtime_id: str | None, action: str,
                    from_state: str | None, to_state: str | None,
                    detail: dict | None, by: str | None) -> None: ...


class SqliteIndexStore:
    """本地 SQLite 实现（缓存，可重建）。"""

    def __init__(self, session: Session) -> None:
        self._s = session

    # ---- skills ----

    def upsert_skill(self, skill: SkillRecord) -> SkillRecord:
        row = self._s.scalar(select(Skill).where(Skill.slug == skill.slug))
        if row is None:
            row = Skill(id=skill.id or _ulid(), slug=skill.slug, name=skill.name)
            self._s.add(row)
        row.name = skill.name
        row.description = skill.description
        row.category = skill.category
        # tags 全量替换
        for t in self._s.scalars(select(SkillTag).where(SkillTag.skill_id == row.id)):
            self._s.delete(t)
        for tag in skill.tags:
            self._s.add(SkillTag(skill_id=row.id, tag=tag))
        self._s.commit()
        return SkillRecord(id=row.id, slug=row.slug, name=row.name,
                           description=row.description, category=row.category,
                           tags=list(skill.tags), created_at=row.created_at,
                           deprecated=bool(row.deprecated))

    def get_skill_by_slug(self, slug: str) -> SkillRecord | None:
        row = self._s.scalar(select(Skill).where(Skill.slug == slug))
        if row is None:
            return None
        tags = list(self._s.scalars(select(SkillTag.tag).where(SkillTag.skill_id == row.id)))
        return SkillRecord(id=row.id, slug=row.slug, name=row.name,
                           description=row.description, category=row.category, tags=tags,
                           created_at=row.created_at, deprecated=bool(row.deprecated))

    def set_skill_deprecated(self, skill_id: str, deprecated: bool) -> bool:
        """N-07.3 / N-07.5：置 / 清弃用标记。返回是否发生变化（幂等可重放）。"""
        row = self._s.get(Skill, skill_id)
        if row is None:
            return False
        changed = bool(row.deprecated) != bool(deprecated)
        row.deprecated = bool(deprecated)
        self._s.commit()
        return changed

    def list_deprecated_slugs(self) -> list[str]:
        return list(self._s.scalars(
            select(Skill.slug).where(Skill.deprecated.is_(True)).order_by(Skill.slug)
        ))

    def list_skills(self, q: SkillQuery) -> list[SkillRecord]:
        stmt = select(Skill).order_by(Skill.slug).limit(q.limit).offset(q.offset)
        if q.q:
            like = f"%{q.q}%"
            stmt = stmt.where((Skill.name.like(like)) | (Skill.description.like(like)))
        if q.category:
            stmt = stmt.where(Skill.category == q.category)
        rows = list(self._s.scalars(stmt))
        return [
            SkillRecord(id=r.id, slug=r.slug, name=r.name,
                        description=r.description, category=r.category,
                        created_at=r.created_at, deprecated=bool(r.deprecated))
            for r in rows
        ]

    def delete_bindings(self, skill_id: str) -> None:
        """删除该 Skill 的全部 binding（P3 CRUD：删除 Skill 时级联清理，
        SQLite 默认不启用 FK 级联，须显式删）。"""
        for b in self._s.scalars(
            select(RuntimeBinding).where(RuntimeBinding.skill_id == skill_id)
        ):
            self._s.delete(b)
        self._s.commit()

    def delete_skill(self, skill_id: str) -> None:
        row = self._s.get(Skill, skill_id)
        if row is not None:
            # 上游源随 skill 一起清（同一条事实，留着就是孤儿行）
            src = self._s.scalar(
                select(UpstreamSource).where(UpstreamSource.skill_id == skill_id)
            )
            if src is not None:
                self._s.delete(src)
            # 附表行一并清理（SQLite 默认无 FK 级联）
            for t in self._s.scalars(
                select(SkillTag).where(SkillTag.skill_id == skill_id)
            ):
                self._s.delete(t)
            self._s.delete(row)
            self._s.commit()

    # ---- runtimes ----

    def upsert_runtime(self, runtime_id: str, key: str, adapter_key: str, root_path: Path,
                       scope: str = "user") -> None:
        row = self._s.get(Runtime, runtime_id)
        if row is None:
            row = Runtime(id=runtime_id, key=key)
            self._s.add(row)
        row.key = key
        row.adapter_key = adapter_key
        row.root_path = str(root_path)
        row.scope = scope
        self._s.commit()

    def list_runtimes(self) -> list[Runtime]:
        return list(self._s.scalars(select(Runtime).order_by(Runtime.key)))

    # ---- 上游来源（FR-1.0.13 / E-08）----

    @staticmethod
    def _upstream_record(row: UpstreamSource) -> UpstreamSourceRecord:
        return UpstreamSourceRecord(
            skill_id=row.skill_id, type=row.type, url=row.url, ref=row.ref,
            base_version=row.base_version, upstream_hash=row.upstream_hash,
            last_checked_at=row.last_checked_at, status=row.status,
            consecutive_failures=int(row.consecutive_failures or 0),
        )

    def get_upstream_source(self, skill_id: str) -> UpstreamSourceRecord | None:
        row = self._s.scalar(
            select(UpstreamSource).where(UpstreamSource.skill_id == skill_id)
        )
        return None if row is None else self._upstream_record(row)

    def list_upstream_sources(self) -> list[UpstreamSourceRecord]:
        return [
            self._upstream_record(r)
            for r in self._s.scalars(select(UpstreamSource))
        ]

    def upsert_upstream_source(self, s: UpstreamSourceRecord) -> None:
        row = self._s.scalar(
            select(UpstreamSource).where(UpstreamSource.skill_id == s.skill_id)
        )
        if row is None:
            row = UpstreamSource(id=_ulid(), skill_id=s.skill_id, type=s.type)
            self._s.add(row)
        row.type = s.type
        row.url = s.url
        row.ref = s.ref
        row.base_version = s.base_version
        row.upstream_hash = s.upstream_hash
        row.last_checked_at = s.last_checked_at
        row.status = s.status
        row.consecutive_failures = int(s.consecutive_failures)
        self._s.commit()

    def set_runtime_enabled(self, key: str, enabled: bool) -> bool:
        """按 key 停用 / 启用（FR-2.4.10）。返回 False = 该 key 未登记。"""
        row = self._s.scalar(select(Runtime).where(Runtime.key == key))
        if row is None:
            return False
        row.enabled = enabled
        self._s.commit()
        return True

    def delete_runtime(self, key: str) -> bool:
        """按 key 删登记行（云端 custom 删除时对齐）。返回 False = 未登记。"""
        row = self._s.scalar(select(Runtime).where(Runtime.key == key))
        if row is None:
            return False
        self._s.delete(row)
        self._s.commit()
        return True

    # ---- bindings ----

    def upsert_binding(self, b: RuntimeBinding) -> RuntimeBinding:
        self._s.merge(b)
        self._s.commit()
        return b

    def get_binding(self, skill_id: str, runtime_id: str) -> RuntimeBinding | None:
        return self._s.scalar(
            select(RuntimeBinding).where(
                RuntimeBinding.skill_id == skill_id,
                RuntimeBinding.runtime_id == runtime_id,
            )
        )

    def list_bindings(self, skill_id: str | None = None) -> list[RuntimeBinding]:
        stmt = select(RuntimeBinding)
        if skill_id:
            stmt = stmt.where(RuntimeBinding.skill_id == skill_id)
        return list(self._s.scalars(stmt))

    # ---- 设备侧开关（V2-T1）----

    def get_setting(self, key: str) -> str | None:
        row = self._s.get(AppSetting, key)
        return row.value if row is not None else None

    def set_setting(self, key: str, value: str) -> None:
        row = self._s.get(AppSetting, key)
        if row is None:
            self._s.add(AppSetting(key=key, value=value))
        else:
            row.value = value
            row.updated_at = _now()
        self._s.commit()

    # ---- 调用遥测（N-11.1）----

    def add_usage_event(self, *, session_id: str, runtime: str, skill_id: str, op: str,
                        via: str, duration_ms: int | None, ok: bool,
                        param_digest: str | None,
                        tokens_in: int = 0, tokens_out: int = 0) -> None:
        self._s.add(SkillUsageEvent(
            id=_ulid(), session_id=session_id, runtime=runtime, skill_id=skill_id,
            op=op, via=via, duration_ms=duration_ms, ok=ok, param_digest=param_digest,
            tokens_in=tokens_in, tokens_out=tokens_out,
        ))
        self._s.commit()

    def list_usage_events(self, since: datetime) -> list[SkillUsageEvent]:
        return list(self._s.scalars(
            select(SkillUsageEvent)
            .where(SkillUsageEvent.at >= since)
            .order_by(SkillUsageEvent.at)
        ))

    def replace_usage_daily(self, since: date, rows: list[dict]) -> int:
        """重算窗口内整体覆盖（TQ-02 幂等口径）：先删该区间，再写新行。

        不用「累加 upsert」：聚合是**从事件表重算**，同一批事件跑两次必须得出同一结果。
        """
        self._s.execute(delete(SkillUsageDaily).where(SkillUsageDaily.date >= since))
        for row in rows:
            self._s.add(SkillUsageDaily(
                id=_ulid(),
                device_id=row["device_id"],
                skill_id=row["skill_id"],
                date=row["date"],
                via=row["via"],
                count=row["count"],
                tokens_in=row.get("tokens_in", 0),
                tokens_out=row.get("tokens_out", 0),
                duration_ms_p50=row.get("duration_ms_p50"),
                duration_ms_p95=row.get("duration_ms_p95"),
                by_op=row.get("by_op"),
            ))
        self._s.commit()
        return len(rows)

    def list_usage_daily(self, since: date) -> list[SkillUsageDaily]:
        return list(self._s.scalars(
            select(SkillUsageDaily)
            .where(SkillUsageDaily.date >= since)
            .order_by(SkillUsageDaily.date, SkillUsageDaily.skill_id, SkillUsageDaily.via)
        ))

    def purge_usage_events(self, before: datetime) -> int:
        """清理 30 天前事件（N-11.1）；只在重算完成后调用（5.2 表注）。"""
        result = self._s.execute(
            delete(SkillUsageEvent).where(SkillUsageEvent.at < before)
        )
        self._s.commit()
        return int(result.rowcount or 0)

    # ---- 检查结果（E-02.6）----

    def replace_decay_checks(self, skill_id: str, rows: list[dict]) -> int:
        """整批替换该 skill 的 decay 行（保留 gate 行）。

        「先清后写」保持「最新一次检查」语义 —— 与云端 record_checks 同口径。
        """
        self._s.execute(delete(SkillCheck).where(
            SkillCheck.skill_id == skill_id, SkillCheck.scope == "decay"
        ))
        for row in rows:
            self._s.add(SkillCheck(
                id=_ulid(),
                skill_id=skill_id,
                scope="decay",
                rule=row["rule"],
                level=row["level"],
                message=row["message"],
                value=row.get("value"),
            ))
        self._s.commit()
        return len(rows)

    def replace_gate_checks(self, skill_id: str, rows: list[dict],
                            *, ruleset_hash: str | None = None) -> int:
        """整批替换该 skill 的 gate 行（保留 decay 行），并写入规则集指纹（N-03.5）。

        与 `replace_decay_checks` 同口径的「最新一次检查」语义：
        先清后写；历史结果的可复现性靠 `ruleset_hash`，不是靠堆历史行。
        """
        self._s.execute(delete(SkillCheck).where(
            SkillCheck.skill_id == skill_id, SkillCheck.scope == "gate"
        ))
        for row in rows:
            self._s.add(SkillCheck(
                id=_ulid(),
                skill_id=skill_id,
                scope="gate",
                rule=row["rule"],
                level=row["level"],
                message=row["message"],
                value=row.get("value"),
                ruleset_hash=ruleset_hash,
            ))
        self._s.commit()
        return len(rows)

    def list_checks(self, scope: str | None = None) -> list[SkillCheck]:
        """当前检查结果（`gate` / `decay` / 全部）。

        体检报告取**落库值**而不是重算：报告是「这一期度量到的状态」，
        重算会让历史报告随代码版本漂移（E-02.6 落库的意义就在这）。
        """
        stmt = select(SkillCheck)
        if scope is not None:
            stmt = stmt.where(SkillCheck.scope == scope)
        return list(self._s.scalars(stmt))

    # ---- 体检报告（N-02.3）----

    def add_health_report(self, *, period_start: datetime, period_end: datetime,
                          payload: dict, unchanged: bool) -> str:
        report_id = _ulid()
        self._s.add(HealthReport(
            id=report_id, device_id="local",
            period_start=period_start, period_end=period_end,
            payload=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            unchanged=unchanged, created_at=_now(),
        ))
        self._s.commit()
        return report_id

    def list_health_reports(self, limit: int = 20) -> list[HealthReport]:
        return list(self._s.scalars(
            select(HealthReport)
            .order_by(HealthReport.period_end.desc())
            .limit(max(1, limit))
        ))

    def get_health_report(self, report_id: str) -> HealthReport | None:
        return self._s.get(HealthReport, report_id)

    # ---- 单 skill 调用记录（N-11.7）----

    def list_skill_events(self, skill_id: str, limit: int = 100) -> list[SkillUsageEvent]:
        """近 N 次调用（时间倒序）。事件表保留 30 天，更早的记录已按设计清理。"""
        return list(self._s.scalars(
            select(SkillUsageEvent)
            .where(SkillUsageEvent.skill_id == skill_id)
            .order_by(SkillUsageEvent.at.desc())
            .limit(max(1, min(limit, 500)))
        ))

    def list_events_by_sessions(self, session_ids: list[str],
                                per_session: int = 50) -> list[SkillUsageEvent]:
        """按 session 批量取事件（N-11.7 L3 调用链）：时间升序还原 A→B→error。"""
        if not session_ids:
            return []
        return list(self._s.scalars(
            select(SkillUsageEvent)
            .where(SkillUsageEvent.session_id.in_(session_ids))
            .order_by(SkillUsageEvent.at.asc())
            .limit(max(1, per_session) * len(session_ids))
        ))

    # ---- 相似对（E-04.2 / E-04.4）----

    def replace_similarity(self, pairs: list[tuple[str, str, float, str]]) -> int:
        """覆盖式落相似对（键 = skill_id 对）：返回新增行数。

        幂等靠两点：同对只更新分数（不新增行），**不再相似的对要删掉** ——
        否则"改完之后相似度掉到阈值以下"的提示会永远留在面板上。
        """
        existing = {
            (row.skill_a_id, row.skill_b_id): row
            for row in self._s.scalars(select(SkillSimilarity))
        }
        added = 0
        for skill_a, skill_b, score, method in pairs:
            row = existing.pop((skill_a, skill_b), None)
            if row is None:
                self._s.add(SkillSimilarity(
                    skill_a_id=skill_a, skill_b_id=skill_b,
                    score=score, method=method,
                ))
                added += 1
            else:
                row.score = score
                row.method = method
        for row in existing.values():
            self._s.delete(row)
        self._s.commit()
        return added

    def list_similarity(self, threshold: float = 0.0) -> list[tuple[str, str, str, str, float]]:
        """相似对：`(a_slug, b_slug, a_id, b_id, score)`，直接给展示层用。"""
        a = aliased(Skill)
        b = aliased(Skill)
        stmt = (
            select(a.slug, b.slug, a.id, b.id, SkillSimilarity.score)
            .select_from(SkillSimilarity)
            .join(a, a.id == SkillSimilarity.skill_a_id)
            .join(b, b.id == SkillSimilarity.skill_b_id)
            .where(SkillSimilarity.score >= threshold)
            .order_by(SkillSimilarity.score.desc(), a.slug, b.slug)
        )
        return [(sa, sb, ia, ib, float(score)) for sa, sb, ia, ib, score in
                self._s.execute(stmt).all()]

    def list_dependency_edges(self) -> list[tuple[str, str]]:
        """依赖图的边集合（E-02.1 反向入度用）。未登记的对端 slug 不返回。"""
        owner = aliased(Skill)
        dep = aliased(Skill)
        stmt = (
            select(owner.slug, dep.slug)
            .join(SkillDependency, SkillDependency.skill_id == owner.id)
            .join(dep, dep.id == SkillDependency.depends_on_skill_id)
        )
        return [(a, b) for a, b in self._s.execute(stmt).all()]

    # ---- sync 审计（NFR-9）----

    def record_sync(self, skill_id: str | None, runtime_id: str | None, action: str,
                    from_state: str | None = None, to_state: str | None = None,
                    detail: dict | None = None, by: str | None = None) -> None:
        self._s.add(SyncRecord(
            id=_ulid(), skill_id=skill_id, runtime_id=runtime_id, action=action,
            from_state=from_state, to_state=to_state, detail=detail, at=_now(), by=by,
        ))
        self._s.commit()


class PostgresIndexStore(SqliteIndexStore):
    """云端 Postgres 实现（表结构同构，检索用 pg_trgm GIN 索引）。实现待 P3。"""
