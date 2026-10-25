"""开发期 `init_schema` 的自愈能力（FR-2.4.10 / T25）。

`create_all` 只建**缺的表**，对已存在的表既不加列也不换 CHECK。所以凡是"已存在的表"
的结构变更，都必须由 `store/db.py::_migrate` 幂等补齐 —— 这里守的就是这条路径。

两个已发生的真实故障：

1. `change_requests.ck_cr_status` 停在三态（旧库），approve / reject 写库撞
   `CHECK constraint failed` —— 审批链路整条不可用；
2. `skill_checks.ruleset_hash` 曾只靠"CHECK 重建顺带带出来"，若某库 CHECK 已是
   新值却缺这一列，`_migrate` 补不到，ORM 查询报 `no such column`。

用真实文件库（不是内存库）+ 手工回退表结构来复现，因为内存库每次都是新 DDL，
永远不会暴露这类漂移。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import select

from skillos.store import models
from skillos.store.db import init_schema, make_engine, make_session_factory

_CR_LEGACY_DDL = """
CREATE TABLE change_requests__old (
    id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    base_version TEXT,
    target_commit TEXT,
    title TEXT NOT NULL,
    body TEXT,
    status TEXT NOT NULL,
    author_id TEXT,
    reviewer_id TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_cr_status CHECK (status IN ('open','merged','closed'))
)
"""

# 新 CHECK 已在、只缺 ruleset_hash —— 恰是"重建不触发"的那个状态
_SKILL_CHECKS_LEGACY_DDL = """
CREATE TABLE skill_checks (
    id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    version_id TEXT,
    scope TEXT NOT NULL DEFAULT 'gate',
    rule TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    value TEXT,
    at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_check_level CHECK (level IN ('error','warn','info','unknown')),
    CONSTRAINT ck_check_scope CHECK (scope IN ('gate','decay'))
)
"""


def _ddl(db: Path, table: str) -> str:
    con = sqlite3.connect(db)
    row = con.execute(
        "select sql from sqlite_master where type='table' and name=?", (table,)
    ).fetchone()
    con.close()
    return (row[0] if row else "") or ""


def _downgrade_cr_check(db: Path) -> None:
    con = sqlite3.connect(db)
    con.executescript(_CR_LEGACY_DDL)
    con.execute(
        "INSERT INTO change_requests__old "
        "SELECT id, team_id, skill_id, base_version, target_commit, title, body, "
        "status, author_id, reviewer_id, created_at, updated_at FROM change_requests"
    )
    con.execute("DROP TABLE change_requests")
    con.execute("ALTER TABLE change_requests__old RENAME TO change_requests")
    con.commit()
    con.close()


class TestChangeRequestStatusCheck:
    def test_legacy_three_state_check_is_upgraded(self, tmp_path: Path) -> None:
        """旧三态 CHECK 的库，`init_schema` 之后必须写得进 approved / rejected。"""
        db = tmp_path / "legacy.db"
        engine = make_engine(f"sqlite:///{db}")
        init_schema(engine)

        session = make_session_factory(engine)()
        session.add(models.ChangeRequest(
            id="cr1", team_id="t1", skill_id="s1", title="旧行", status="open",
        ))
        session.commit()
        session.close()

        _downgrade_cr_check(db)
        assert "'approved'" not in _ddl(db, "change_requests")   # 前置：确实是旧库

        init_schema(engine)   # 自愈

        ddl = _ddl(db, "change_requests")
        assert "'approved'" in ddl and "'rejected'" in ddl
        # 历史行原样保留，状态语义未变
        con = sqlite3.connect(db)
        assert con.execute("select status from change_requests").fetchall() == [("open",)]
        con.execute("update change_requests set status='approved'")
        con.execute("update change_requests set status='rejected'")
        con.commit()
        con.close()

    def test_upgrade_is_idempotent(self, tmp_path: Path) -> None:
        """重复启动不该反复重建表（第三次调用后结构与数据都不变）。"""
        db = tmp_path / "idem.db"
        engine = make_engine(f"sqlite:///{db}")
        init_schema(engine)
        _downgrade_cr_check(db)

        init_schema(engine)
        first = _ddl(db, "change_requests")
        init_schema(engine)
        assert _ddl(db, "change_requests") == first

    def test_illegal_status_still_rejected(self, tmp_path: Path) -> None:
        """换约束不是放开约束：库层第二道闸必须继续拦住非法取值。"""
        db = tmp_path / "gate.db"
        engine = make_engine(f"sqlite:///{db}")
        init_schema(engine)
        _downgrade_cr_check(db)
        init_schema(engine)

        con = sqlite3.connect(db)
        try:
            con.execute(
                "insert into change_requests "
                "(id, team_id, skill_id, title, status, created_at, updated_at) "
                "values ('x','t','s','t','bogus','2026-01-01 00:00:00','2026-01-01 00:00:00')"
            )
            con.commit()
            raise AssertionError("非法 status 不该写进去")
        except sqlite3.IntegrityError as exc:
            assert "ck_cr_status" in str(exc)
        finally:
            con.close()


class TestSkillCheckColumns:
    def test_ruleset_hash_added_without_check_rebuild(self, tmp_path: Path) -> None:
        """CHECK 已是新值但缺 ruleset_hash 的库，也要能被 `_migrate` 补齐。"""
        db = tmp_path / "checks.db"
        con = sqlite3.connect(db)
        con.executescript(_SKILL_CHECKS_LEGACY_DDL)
        con.commit()
        con.close()

        engine = make_engine(f"sqlite:///{db}")
        init_schema(engine)

        session = make_session_factory(engine)()
        # 缺列时这条查询会 `no such column: skill_checks.ruleset_hash`
        assert session.execute(select(models.SkillCheck)).all() == []
        session.close()
