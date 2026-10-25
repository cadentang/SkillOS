"""M5：`upstream_sources.consecutive_failures`（E-08.4）。

连续 3 次网络失败才置 `status=error`（下个周期重试），单次抖动不降级。
默认 0 ⇒ 历史行语义不变。

空库例外同 0005：`upstream_sources` 由 `create_all` 建表，空库上跑链时不存在 ⇒ no-op。

Revision ID: 0008_m5_upstream_failures
Revises: 0007_m4_skill_deprecated
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_m5_upstream_failures"
down_revision = "0007_m4_skill_deprecated"
branch_labels = None
depends_on = None


def _table_exists() -> bool:
    return sa.inspect(op.get_bind()).has_table("upstream_sources")


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        c["name"] == "consecutive_failures"
        for c in inspector.get_columns("upstream_sources")
    )


def upgrade() -> None:
    if not _table_exists() or _has_column():
        return
    op.add_column(
        "upstream_sources",
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default="0",
        ),
    )


def downgrade() -> None:
    if not _table_exists() or not _has_column():
        return
    op.drop_column("upstream_sources", "consecutive_failures")
