"""M2 revision ①d：`health_reports`（N-02.3 体检报告）。

**本地表**：报告由 daemon / CLI 生成，云端不碰磁盘（红线 5）；Web 通过 daemon 的
`report_list` / `report_get` 具名 op 读取。

`payload` 同时承担展示与基线两种角色（N-02.2 要算"自上次以来新增了什么"，
基线只能来自上一份报告），`unchanged` 独立成列供列表筛选（N-02.6）。

Revision ID: 0004_m2_health_reports
Revises: 0003_m2_event_tokens
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_m2_health_reports"
down_revision = "0003_m2_event_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "health_reports",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("device_id", sa.Text(), nullable=False, server_default="local"),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("unchanged", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_health_reports_created", "health_reports", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_health_reports_created", table_name="health_reports")
    op.drop_table("health_reports")
