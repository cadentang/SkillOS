"""M3 revision ③：`skill_checks.ruleset_hash`（N-03.5）。

规则集变更后要能回答"这条 finding 是哪一版规则集判出来的"。只加列、不回填：
历史行 `NULL` 表示"当时还没有规则集指纹"，不要用当前 hash 覆盖（那会伪造历史）。

**空库例外**：`skill_checks` 由 `init_schema()`（`create_all`）建表，不在迁移链里。
空库上跑整条链时该表尚不存在 ⇒ 本迁移 no-op；真实升级（老库已有表）时才 ALTER。
没有这个守卫，`upgrade head` 在空库上必死在 `no such table`（M2 的
`test_usage_schema.TestAlembicRevision` 就是这条链的守门人）。

Revision ID: 0005_m3_ruleset_hash
Revises: 0004_m2_health_reports
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_m3_ruleset_hash"
down_revision = "0004_m2_health_reports"
branch_labels = None
depends_on = None


def _table_exists() -> bool:
    return sa.inspect(op.get_bind()).has_table("skill_checks")


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == "ruleset_hash" for c in inspector.get_columns("skill_checks"))


def upgrade() -> None:
    # 表不存在（空库）或列已存在（重复升级）→ 都不该 ALTER
    if not _table_exists() or _has_column():
        return
    op.add_column("skill_checks", sa.Column("ruleset_hash", sa.Text(), nullable=True))


def downgrade() -> None:
    if not _table_exists() or not _has_column():
        return
    op.drop_column("skill_checks", "ruleset_hash")
