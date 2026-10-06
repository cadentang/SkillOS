"""索引读写（04 §2.3）。两个实现：SqliteIndexStore（本地）、PostgresIndexStore（云端）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.orm import Session

from skillos.store.models import RuntimeBinding


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
    def upsert_skill(self, skill: SkillRecord) -> None: ...
    def list_skills(self, q: SkillQuery) -> list[SkillRecord]: ...
    def get_skill(self, skill_id: str) -> SkillRecord | None: ...
    # bindings
    def upsert_binding(self, b: RuntimeBinding) -> None: ...
    def list_bindings(self, skill_id: str | None = None) -> list[RuntimeBinding]: ...


class SqliteIndexStore:
    """本地 SQLite 实现。实现待 P1（含 FTS5 全文检索，04 §3.3）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_skill(self, skill: SkillRecord) -> None:
        raise NotImplementedError("P1 实现")

    def list_skills(self, q: SkillQuery) -> list[SkillRecord]:
        raise NotImplementedError("P1 实现（FTS5：name + description + tags）")

    def get_skill(self, skill_id: str) -> SkillRecord | None:
        raise NotImplementedError("P1 实现")

    def upsert_binding(self, b: RuntimeBinding) -> None:
        raise NotImplementedError("P1 实现")

    def list_bindings(self, skill_id: str | None = None) -> list[RuntimeBinding]:
        raise NotImplementedError("P1 实现")


class PostgresIndexStore(SqliteIndexStore):
    """云端 Postgres 实现（表结构同构，检索用 pg_trgm GIN 索引）。实现待 P3。"""
