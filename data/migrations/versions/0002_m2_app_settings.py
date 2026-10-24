"""M2 revision ①b：`app_settings`（V2-T1，从 revision ② 提前）。

为什么不并进 0001：0001 只放遥测数据表；开关表在规格里归属 revision ②（M4 协作），
但 M2 的采集开关与 L2 路径表单都依赖它，所以单独一个可独立回滚的 revision 提前落地。

Revision ID: 0002_m2_settings
Revises: 0001_m2_usage
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_m2_settings"
down_revision = "0001_m2_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
