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
        # E-02.6：折旧结果与门禁 findings 同表，用 scope 区分；value 存原始测量值
        "skill_checks": [
            ("scope", "ALTER TABLE skill_checks ADD COLUMN scope TEXT NOT NULL DEFAULT 'gate'"),
            ("value", "ALTER TABLE skill_checks ADD COLUMN value TEXT"),
        ],
        # L2 会话适配器回填的 token 估算值（N-11.3）：落在事件行上，聚合才能重算
        "skill_usage_events": [
            ("tokens_in",
             "ALTER TABLE skill_usage_events ADD COLUMN tokens_in INTEGER DEFAULT 0"),
            ("tokens_out",
             "ALTER TABLE skill_usage_events ADD COLUMN tokens_out INTEGER DEFAULT 0"),
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
        _upgrade_skill_checks_constraints(conn, engine.dialect.name)


def _upgrade_skill_checks_constraints(conn, dialect_name: str) -> None:
    """把 skill_checks 的 CHECK 约束升到支持 `level='unknown'` / `scope`（E-01.3 / E-02.6）。

    SQLite 不支持 ALTER CHECK，只能重建表；Postgres 直接换约束。
    幂等：检测到新约束已存在就什么都不做。**不做数据迁移** —— 旧行原样保留，
    `scope` 缺失时视为 `gate`（DDL 默认值已保证）。
    """
    from sqlalchemy.schema import CreateTable

    from skillos.store import models

    if dialect_name == "sqlite":
        row = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='skill_checks'"
        ).fetchone()
        if row is None:
            return  # 表还没建（create_all 会带新约束）
        ddl = row[0] or ""
        if "'unknown'" in ddl and "scope" in ddl:
            return
        new_table = "skill_checks__new"
        create_sql = str(CreateTable(models.SkillCheck.__table__).compile(conn))
        create_sql = create_sql.replace(
            "CREATE TABLE skill_checks", f"CREATE TABLE {new_table}", 1
        )
        cols = "id, skill_id, version_id, scope, rule, level, message, value, at"
        conn.exec_driver_sql(create_sql)
        conn.exec_driver_sql(
            f"INSERT INTO {new_table} ({cols}) SELECT {cols} FROM skill_checks"
        )
        conn.exec_driver_sql("DROP TABLE skill_checks")
        conn.exec_driver_sql(f"ALTER TABLE {new_table} RENAME TO skill_checks")
        return

    # postgres / 其它：约束可替换
    conn.exec_driver_sql("ALTER TABLE skill_checks DROP CONSTRAINT IF EXISTS ck_check_level")
    conn.exec_driver_sql(
        "ALTER TABLE skill_checks ADD CONSTRAINT ck_check_level "
        "CHECK (level IN ('error','warn','info','unknown'))"
    )
    conn.exec_driver_sql("ALTER TABLE skill_checks DROP CONSTRAINT IF EXISTS ck_check_scope")
    conn.exec_driver_sql(
        "ALTER TABLE skill_checks ADD CONSTRAINT ck_check_scope "
        "CHECK (scope IN ('gate','decay'))"
    )
