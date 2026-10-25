"""M4：`skills.deprecated` 弃用软状态（N-07.3）。

回收落选方不删 canonical，只置标记 + 回收 runtime 副本；重新 push 即恢复（N-07.5）。
默认 false ⇒ 历史行语义不变，无需回填。

空库例外同 0005：`skills` 由 `create_all` 建表，空库上跑链时不存在 ⇒ no-op。

Revision ID: 0007_m4_skill_deprecated
Revises: 0006_m4_cr_status
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_m4_skill_deprecated"
down_revision = "0006_m4_cr_status"
branch_labels = None
depends_on = None


def _table_exists() -> bool:
    return sa.inspect(op.get_bind()).has_table("skills")


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == "deprecated" for c in inspector.get_columns("skills"))


def upgrade() -> None:
    if not _table_exists() or _has_column():
        return
    op.add_column(
        "skills",
        sa.Column("deprecated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    if not _table_exists() or not _has_column():
        return
    op.drop_column("skills", "deprecated")
