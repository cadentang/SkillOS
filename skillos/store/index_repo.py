"""索引读写（04 §2.3）。两个实现：SqliteIndexStore（本地）、PostgresIndexStore（云端）。

本地 SQLite 是缓存不是事实源（FR-1.0.30）：可随时从 git 仓库 + 磁盘重建。
检索第一版用 LIKE（FTS5 全文检索排期 P2 接 Web 搜索时补）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from skillos.store.models import (
    Runtime,
    RuntimeBinding,
    Skill,
    SkillCheck,
    SkillDependency,
    SkillTag,
    SyncRecord,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _ulid() -> str:
    return uuid.uuid4().hex


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


class IndexStore(Protocol):
    """数据访问协议（04 §2.3）。"""

    # skills
    def upsert_skill(self, skill: SkillRecord) -> SkillRecord: ...
    def get_skill_by_slug(self, slug: str) -> SkillRecord | None: ...
    def list_skills(self, q: SkillQuery) -> list[SkillRecord]: ...
    def delete_skill(self, skill_id: str) -> None: ...
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
    # 检查结果（E-02.6）：折旧维度一行一个，写入前先清该 skill 的旧 decay 行
    def replace_decay_checks(self, skill_id: str, rows: list[dict]) -> int: ...
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
                           tags=list(skill.tags))

    def get_skill_by_slug(self, slug: str) -> SkillRecord | None:
        row = self._s.scalar(select(Skill).where(Skill.slug == slug))
        if row is None:
            return None
        tags = list(self._s.scalars(select(SkillTag.tag).where(SkillTag.skill_id == row.id)))
        return SkillRecord(id=row.id, slug=row.slug, name=row.name,
                           description=row.description, category=row.category, tags=tags)

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
                        description=r.description, category=r.category)
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

    # ---- 检查结果（E-02.6）----

    def replace_decay_checks(self, skill_id: str, rows: list[dict]) -> int:
        """整批替换该 skill 的 decay 行（保留 gate 行）。

        「先清后写」保持「最新一次检查」语义 —— 与云端 record_checks 同口径。
        """
        for old in self._s.scalars(
            select(SkillCheck).where(
                SkillCheck.skill_id == skill_id, SkillCheck.scope == "decay"
            )
        ):
            self._s.delete(old)
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
