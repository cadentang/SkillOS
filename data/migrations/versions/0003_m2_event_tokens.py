"""M2 revision ①c：事件表加 token 列（N-11.3 / TQ-02）。

L2 会话适配器算出的 token 是**估算值**，必须落在事件行上而不是只写日聚合：
`usage_aggregate` 是「以事件表重算 + 窗口内整体覆盖」，只写日聚合的话下一次重算就抹掉了。

旧行默认 0（= 没有 L2 数据），不回填、不估算 —— 历史 token 无从考证。

Revision ID: 0003_m2_event_tokens
Revises: 0002_m2_settings
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_m2_event_tokens"
down_revision = "0002_m2_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "skill_usage_events",
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "skill_usage_events",
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("skill_usage_events", "tokens_out")
    op.drop_column("skill_usage_events", "tokens_in")
