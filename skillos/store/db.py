"""引擎与会话（SQLite / Postgres 同构，04 §3.4）。

本地 SQLite：文件由调用方给定路径（T1：不从环境变量隐式取）。
云端 Postgres：URL 由部署配置注入。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(url: str) -> Engine:
    """按 URL 创建引擎。sqlite 需启用外键约束（默认关闭）。"""
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url)


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path}"


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_schema(engine: Engine) -> None:
    """开发期直接建表 + 幂等轻量级 migration；正式口径走 Alembic（data/migrations）。

    _migrate 负责对**已存在的 db**补缺失列（如 runtimes.discover_only）；不存在则跳过。
    create_all 对新 db 已会带新列（FR-2.4.10 / T25）。
    """
    from skillos.store import models  # noqa: F401 —— 触发模型注册

    models.Base.metadata.create_all(engine)
    _migrate(engine)


def _migrate(engine: Engine) -> None:
    """幂等轻量级 migration：云端 + daemon 端共用（FR-2.4.10 / T25）。

    用方言分支：sqlite PRAGMA table_info；postgres information_schema。
    仅补"显式列存在性"——不做数据迁移 / 不改类型 / 不删列。
    """
    needed: dict[str, list[tuple[str, str]]] = {
        # table → [(column, ddl), ...]
        "runtimes": [
            ("discover_only", "ALTER TABLE runtimes ADD COLUMN discover_only BOOLEAN DEFAULT 0"),
        ],
    }
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            for table, cols in needed.items():
                existing = {
                    row[1]
                    for row in conn.exec_driver_sql(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                for col, ddl in cols:
                    if col not in existing:
                        conn.exec_driver_sql(ddl)
        else:
            # postgres / other
            for table, cols in needed.items():
                existing = {
                    row[0]
                    for row in conn.exec_driver_sql(
                        f"SELECT column_name FROM information_schema.columns "
                        f"WHERE table_name = '{table}'"
                    ).fetchall()
                }
                for col, ddl in cols:
                    if col not in existing:
                        conn.exec_driver_sql(ddl)
