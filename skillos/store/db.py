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
    """开发期直接建表；正式口径走 Alembic（data/migrations）。"""
    from skillos.store import models  # noqa: F401 —— 触发模型注册

    models.Base.metadata.create_all(engine)
