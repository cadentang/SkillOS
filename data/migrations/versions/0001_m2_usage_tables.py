"""M2 revision ①：遥测两张表（N-01 / N-11）。

`docs/12` §3 Revision ①。两张表都是**纯加法**，无数据回填：

- `skill_usage_daily`：本地与云端同构；冲突键 `(device_id, skill_id, date, via)`
  是云端合并与补报去重的落点（V2-T9），因此唯一约束必须存在，PG / SQLite 同名。
- `skill_usage_events`：**仅本地**，聚合的唯一事实源，保留 30 天滚动清理。

Revision ID: 0001_m2_usage
Revises: 无（本仓库首个 Alembic revision；此前表结构由 create_all 建立）
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_m2_usage"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_usage_daily",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("via", sa.Text(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms_p50", sa.Integer(), nullable=True),
        sa.Column("duration_ms_p95", sa.Integer(), nullable=True),
        sa.Column("by_op", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("device_id", "skill_id", "date", "via", name="uq_usage_daily"),
    )
    op.create_index("ix_usage_daily_skill", "skill_usage_daily", ["skill_id", "date"])

    op.create_table(
        "skill_usage_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("runtime", sa.Text(), nullable=False, server_default=""),
        sa.Column("skill_id", sa.Text(), nullable=False),
        sa.Column("op", sa.Text(), nullable=False),
        sa.Column("via", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("param_digest", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_usage_events_skill_at", "skill_usage_events", ["skill_id", "at"])
    op.create_index("ix_usage_events_session", "skill_usage_events", ["session_id", "at"])


def downgrade() -> None:
    op.drop_index("ix_usage_events_session", table_name="skill_usage_events")
    op.drop_index("ix_usage_events_skill_at", table_name="skill_usage_events")
    op.drop_table("skill_usage_events")
    op.drop_index("ix_usage_daily_skill", table_name="skill_usage_daily")
    op.drop_table("skill_usage_daily")
