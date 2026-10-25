"""M4 revision ②：`change_requests.status` 五态（E-05.1）。

`open → approved → merged` / `open → rejected` / `open/approved → closed`。
旧库只有三态 CHECK，**必须重建约束**才能写 `approved` / `rejected` ——
SQLite 不支持 ALTER CHECK，故走 `batch_alter_table`（recreate）；PG 上是普通
DROP/ADD CONSTRAINT。

只改约束、不改数据：历史行里 `open/merged/closed` 在新枚举内仍然合法，
无需数据回填（旧值语义未变）。

空库例外同 0005：`change_requests` 由 `create_all` 建表，空库上跑链时表不存在 → no-op。

Revision ID: 0006_m4_cr_status
Revises: 0005_m3_ruleset_hash
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_m4_cr_status"
down_revision = "0005_m3_ruleset_hash"
branch_labels = None
depends_on = None

_NEW = "status IN ('open','approved','rejected','merged','closed')"
_OLD = "status IN ('open','merged','closed')"


def _table_exists() -> bool:
    return sa.inspect(op.get_bind()).has_table("change_requests")


def upgrade() -> None:
    if not _table_exists():
        return
    with op.batch_alter_table("change_requests", recreate="always") as batch:
        batch.drop_constraint("ck_cr_status", type_="check")
        batch.create_check_constraint("ck_cr_status", _NEW)


def downgrade() -> None:
    if not _table_exists():
        return
    # 回滚前必须先处理取值：新状态在旧枚举里不合法，否则重建时直接失败。
    # 合并回滚口径保守：approved 退 open（未合入事实），rejected 退 closed（已终结）。
    op.execute("UPDATE change_requests SET status='open' WHERE status='approved'")
    op.execute("UPDATE change_requests SET status='closed' WHERE status='rejected'")
    with op.batch_alter_table("change_requests", recreate="always") as batch:
        batch.drop_constraint("ck_cr_status", type_="check")
        batch.create_check_constraint("ck_cr_status", _OLD)
