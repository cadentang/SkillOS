"""M4-04 回收落选方（N-07.3 / N-07.4 / N-07.5）。

核心语义必须一条一条守住：

- 回收**不删 canonical**：正文、历史、可恢复性一律保留（N-07.3）；
- removals 先列后动（红线 16）：未逐条确认 → 整体拒绝，磁盘一个字节不动；
- 回收前落备份（可撤销）；
- 落 `sync_records`（N-07.4）；
- 重新 push 即恢复（N-07.5）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillos.api.main import create_app
from skillos.services.engine import EnvPaths, LocalEngine

_DEMO = (
    b"---\nname: demo\nversion: 1.0.0\n"
    b"description: a demo skill doing useful everyday work\n---\nbody\n"
)
_OTHER = (
    b"---\nname: other\nversion: 1.0.0\n"
    b"description: another skill doing useful everyday work\n---\nbody2\n"
)


@pytest.fixture()
def env(tmp_path: Path):
    home = tmp_path / "home"
    for slug, body in (("demo", _DEMO), ("other", _OTHER)):
        d = home / ".claude" / "skills" / slug
        (d / "references").mkdir(parents=True)
        (d / "SKILL.md").write_bytes(body)
        (d / "references" / "guide.md").write_bytes(b"guide\n")
    engine = LocalEngine(
        EnvPaths(repo_root=tmp_path / "repo", db_path=tmp_path / "state" / "skillos.db"),
        home=home,
    )
    engine.init_env()
    engine.import_skills()
    assert engine.push_skill("demo", runtime_keys=["claude"]).ok
    return engine, home


class TestDeprecate:
    def test_dry_run_lists_removals_without_touching_disk(self, env) -> None:
        engine, home = env
        plan = engine.deprecate_skill("demo", dry_run=True)
        assert plan["dry_run"] is True
        assert plan["removals"], "部署副本存在时必须列出 removals"
        assert all(p.startswith("claude:") for p in plan["removals"])
        # 磁盘原封不动
        for rel in plan["removals"]:
            assert (home / ".claude" / "skills" / "demo" / rel.split(":", 1)[1]).is_file()

    def test_unconfirmed_removals_rejected(self, env) -> None:
        """红线 16：确认集合与实际 removals 不等 → 整体拒绝，不删任何东西。"""
        engine, home = env
        result = engine.deprecate_skill("demo", dry_run=False, confirmed_removals=[])
        assert result["ok"] is False
        assert result["error_code"] == "REMOVALS_UNCONFIRMED"
        assert result["removals"]
        assert (home / ".claude" / "skills" / "demo" / "SKILL.md").is_file()

    def test_deprecate_keeps_canonical_and_records_sync(self, env) -> None:
        engine, home = env
        plan = engine.deprecate_skill("demo", dry_run=True)
        result = engine.deprecate_skill(
            "demo", dry_run=False, confirmed_removals=plan["removals"],
        )
        assert result["ok"] and result["deprecated"] is True

        # runtime 副本回收
        assert not (home / ".claude" / "skills" / "demo").exists()
        # canonical 一条不动（N-07.3）
        assert (engine.paths.repo_root / "skills" / "demo" / "SKILL.md").is_file()
        # binding 回收 + deprecated 标记
        row = engine.db.get_skill_by_slug("demo")
        assert row is not None and row.deprecated is True
        assert engine.db.list_bindings(row.id) == []
        assert "demo" in engine.deprecated_skills()
        assert "other" not in engine.deprecated_skills()
        # 备份可撤销
        backup = Path(result["backup"])
        assert backup.is_dir() and any(backup.rglob("SKILL.md"))
        # 审计（N-07.4）：action 用既定枚举 resolve，detail.event 标 deprecate
        from skillos.store.models import SyncRecord

        events = [(r.action, (r.detail or {}).get("event"))
                  for r in engine.db._s.query(SyncRecord).all()]  # type: ignore[attr-defined]
        assert ("resolve", "deprecate") in events

    def test_second_dry_run_is_empty_after_recover(self, env) -> None:
        """回收后再预览 → 没有可回收的东西（幂等，不重复报 removals）。"""
        engine, _ = env
        plan = engine.deprecate_skill("demo", dry_run=True)
        engine.deprecate_skill("demo", dry_run=False, confirmed_removals=plan["removals"])
        again = engine.deprecate_skill("demo", dry_run=True)
        assert again["removals"] == []

    def test_push_restores_deprecated(self, env) -> None:
        """N-07.5：重新分发即恢复 —— 清标记、重建 binding、写 restore 审计。"""
        engine, home = env
        plan = engine.deprecate_skill("demo", dry_run=True)
        engine.deprecate_skill("demo", dry_run=False, confirmed_removals=plan["removals"])

        assert engine.push_skill("demo", runtime_keys=["claude"]).ok
        row = engine.db.get_skill_by_slug("demo")
        assert row is not None and row.deprecated is False
        assert engine.deprecated_skills() == []
        assert (home / ".claude" / "skills" / "demo" / "SKILL.md").is_file()
        from skillos.store.models import SyncRecord

        events = [(r.action, (r.detail or {}).get("event"))
                  for r in engine.db._s.query(SyncRecord).all()]  # type: ignore[attr-defined]
        assert ("push", "restore") in events

    def test_unknown_slug_raises(self, env) -> None:
        engine, _ = env
        with pytest.raises(Exception) as ei:
            engine.deprecate_skill("nope", dry_run=True)
        assert "NOT_FOUND" in str(getattr(ei.value, "code", "")) or "nope" in str(ei.value)


# ---------------------------------------------------------------- HTTP 代理


class _FakeHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def submit_task(self, user_id: str, op: str, params: dict) -> dict:
        self.calls.append((op, params))
        return {"ok": True, "data": {"dry_run": True, "removals": ["claude:SKILL.md"]}}


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(
        db_url=f"sqlite:///{tmp_path / 'cloud.db'}", jwt_secret="test-secret",
    )
    with TestClient(app) as c:
        yield c


def _token(client: TestClient) -> str:
    client.post("/api/auth/register", json={"email": "u@x.com", "password": "password123"})
    return client.post("/api/auth/login", json={
        "email": "u@x.com", "password": "password123",
    }).json()["data"]["token"]


class TestResolveRoute:
    def test_dry_run_proxies_drop_slug(self, client: TestClient):
        hub = _FakeHub()
        client.app.state.hub = hub  # type: ignore[attr-defined]
        token = _token(client)
        body = client.post(
            "/api/similarity/demo/other/resolve",
            headers={"Authorization": f"Bearer {token}"},
            json={"keep": "demo"},
        ).json()
        assert body["ok"] is True
        assert body["data"]["removals"] == ["claude:SKILL.md"]
        assert hub.calls == [("skill_deprecate", {
            "slug": "other", "dry_run": True, "confirm_removals": [],
        })]

    def test_keep_must_be_one_of_pair(self, client: TestClient):
        client.app.state.hub = _FakeHub()  # type: ignore[attr-defined]
        token = _token(client)
        r = client.post(
            "/api/similarity/demo/other/resolve",
            headers={"Authorization": f"Bearer {token}"},
            json={"keep": "nobody"},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "INVALID_KEEP"

    def test_offline_is_explicit(self, client: TestClient):
        token = _token(client)
        body = client.post(
            "/api/similarity/demo/other/resolve",
            headers={"Authorization": f"Bearer {token}"},
            json={"keep": "demo", "dry_run": False, "confirm_removals": ["claude:SKILL.md"]},
        ).json()
        assert body["ok"] is False and body["error"]["code"] == "DAEMON_OFFLINE"
