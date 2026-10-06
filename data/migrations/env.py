"""Alembic 迁移环境。URL 从 SKILLOS_DB_URL 读取，默认本地 SQLite。"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from skillos.store import models

config = context.config

if not config.get_main_option("sqlalchemy.url") or os.environ.get("SKILLOS_DB_URL"):
    config.set_main_option("sqlalchemy.url", os.environ.get("SKILLOS_DB_URL", "sqlite:///data/skillos.sqlite3"))

target_metadata = models.Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
