"""M5-01 沙盒演示数据一致性自证（E-06.1 ~ E-06.4）。

守住四件事：

1. seed 产物带 `seed_version` 与期望态声明（E-06.1）；
2. 重置幂等：跑两次终态一致（E-06.2）；
3. **自证**：回读的 skill 数 / 漂移态 / 门禁与声明逐项比对，不一致要显式失败并
   打印差异（E-06.3）—— 静默重置成功比崩溃更糟；
4. 重置是长任务：API 回 202 + task_id，状态可轮询（E-06.4）。
"""
from __future__ import annotations

import importlib.util as iu
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillos.api.main import create_app
from skillos.services.engine import EnvPaths, LocalEngine
from skillos.store.index_repo import SkillQuery

_RUNTIMES = ("claude", "agents")


def _load_seed_script():
    spec = iu.spec_from_file_location("build_sandbox_seed", "scripts/build_sandbox_seed.py")
    mod = iu.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _make_engine(home: Path) -> LocalEngine:
    return LocalEngine(
        EnvPaths(repo_root=home / ".skillos" / "repo",
                 db_path=home / ".skillos" / "skillos.db"),
        home=home, sandbox=True,
    )


@pytest.fixture()
def sandbox(tmp_path: Path):
    """真实 seed 产物（跑 build_sandbox_seed.build）+ 首次启动语义。"""
    import shutil

    mod = _load_seed_script()
    home = tmp_path / "root"
    seed_dir = home / ".skillos-seed"
    mod.build(seed_dir, list(_RUNTIMES))
    for rt in _RUNTIMES:
        shutil.copytree(seed_dir / "runtime" / rt, home / f".{rt}" / "skills",
                        dirs_exist_ok=True)
    engine = _make_engine(home)
    engine.init_env()
    engine.import_skills()
    for slug in ("api-design", "code-review", "daily-report"):
        assert engine.push_skill(slug).ok
    return engine, home, seed_dir


class TestSeedManifest:
    def test_manifest_has_version_and_expectations(self, sandbox) -> None:
        _, _, seed_dir = sandbox
        manifest = json.loads((seed_dir / "seed.json").read_text(encoding="utf-8"))
        assert manifest["seed_version"]
        assert manifest["skills"] == ["api-design", "code-review", "daily-report"]
        assert manifest["expect"]["skill_count"] == 3
        assert manifest["expect"]["gate_has_error"] is False
        assert manifest["expect"]["states"]["daily-report"]

    def test_conflict_overlay_exists(self, sandbox) -> None:
        """剧本要求的 CONFLICT 样例必须有源（reset 在 push 后应用它）。"""
        _, _, seed_dir = sandbox
        overlay = seed_dir / "conflict" / "claude" / "daily-report" / "SKILL.md"
        assert overlay.is_file()
        assert "本地改动" in overlay.read_text(encoding="utf-8")


class TestResetSelfProof:
    def test_reset_reports_seed_version_and_verifies(self, sandbox) -> None:
        engine, _, _ = sandbox
        result = engine.sandbox_reset()
        assert result["verified"] is True
        assert result["diffs"] == []
        assert result["seed_version"]
        assert result["gate_errors"] == []
        assert result["skills"] == ["api-design", "code-review", "daily-report"]

    def test_reset_is_idempotent(self, sandbox) -> None:
        """E-06.2：连跑两次，终态一致（含差异样例落点）。"""
        engine, _, _ = sandbox
        first = engine.sandbox_reset()
        second = engine.sandbox_reset()
        assert first["skills"] == second["skills"]
        assert first["seed_version"] == second["seed_version"]

    def test_conflict_sample_rendered_after_reset(self, sandbox) -> None:
        """E-06 GWT：矩阵里要能看到演示用 CONFLICT（不是全靠用户手动造）。"""
        engine, _, _ = sandbox
        engine.sandbox_reset()
        slug_of = {r.id: r.slug for r in engine.db.list_skills(SkillQuery(limit=1000))}
        states = {
            (slug_of[c.skill_id], c.runtime_key): str(c.state)
            for c in engine.status_matrix()
        }
        assert states[("daily-report", "claude")] == "conflict"
        assert states[("code-review", "claude")] == "synced"

    def test_verify_seed_reports_mismatch(self, sandbox) -> None:
        """声明与实际不符 → 差异项要具体到哪一项（E-06.3）。"""
        engine, _, seed_dir = sandbox
        manifest = json.loads((seed_dir / "seed.json").read_text(encoding="utf-8"))
        manifest["expect"]["skill_count"] = 99
        manifest["expect"]["states"]["code-review"]["claude"] = "outdated"
        diffs = engine.verify_seed(manifest)
        assert any("skill 总数" in d for d in diffs)
        assert any("code-review/claude" in d for d in diffs)

    def test_reset_fails_loudly_on_mismatch(self, sandbox) -> None:
        """自证失败必须抛（E-06.3：不一致 → 启动失败并打印差异）。"""
        engine, _, seed_dir = sandbox
        manifest = json.loads((seed_dir / "seed.json").read_text(encoding="utf-8"))
        manifest["expect"]["skill_count"] = 99
        (seed_dir / "seed.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="沙盒自证失败"):
            engine.sandbox_reset()


class TestResetApi:
    @pytest.fixture()
    def client(self, tmp_path: Path):
        app = create_app(
            db_url=f"sqlite:///{tmp_path / 'cloud.db'}", jwt_secret="test-secret",
            sandbox=True,
        )
        with TestClient(app) as c:
            yield c

    def _token(self, client: TestClient) -> str:
        client.post("/api/auth/register", json={
            "email": "u@x.com", "password": "password123",
        })
        return client.post("/api/auth/login", json={
            "email": "u@x.com", "password": "password123",
        }).json()["data"]["token"]

    def test_offline_is_503(self, client: TestClient):
        token = self._token(client)
        r = client.post("/api/sandbox/reset", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 503 and r.json()["detail"] == "DAEMON_OFFLINE"

    def test_unknown_task_is_404(self, client: TestClient):
        token = self._token(client)
        r = client.get("/api/sandbox/reset/nope",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404

    def test_returns_202_with_task_id(self, client: TestClient):
        """E-06.4：在线时不再同步等结果，而是 202 + task_id；进行中重复点复用同一任务。"""
        import asyncio

        class _Agent:
            user_id = "u1"

        hub = client.app.state.hub
        hub.agents["c1"] = _Agent()  # type: ignore[attr-defined]
        token = self._token(client)
        user_id = client.get("/api/auth/me",
                             headers={"Authorization": f"Bearer {token}"}).json()["data"]["id"]
        hub.agents["c1"].user_id = user_id  # type: ignore[attr-defined]

        async def _slow_submit(user_id: str, op: str, params: dict, timeout=None) -> dict:
            await asyncio.sleep(5)
            return {"ok": True, "data": {"restored": True, "verified": True}}

        hub.submit_task = _slow_submit  # type: ignore[assignment]

        r = client.post("/api/sandbox/reset", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 202, r.text
        data = r.json()["data"]
        assert data["task_id"].startswith("sbx_")
        assert data["status"] == "running"

        # 幂等：重置进行中重复点 → 复用同一 task_id（不并发跑两遍）
        again = client.post("/api/sandbox/reset",
                            headers={"Authorization": f"Bearer {token}"})
        assert again.status_code == 202
        assert again.json()["data"]["task_id"] == data["task_id"]
        assert again.json()["data"]["reused"] is True

        status = client.get(f"/api/sandbox/reset/{data['task_id']}",
                            headers={"Authorization": f"Bearer {token}"})
        assert status.status_code == 200
        assert status.json()["data"]["status"] == "running"


def _load_bootstrap_script():
    spec = iu.spec_from_file_location("sandbox_bootstrap", "scripts/sandbox_bootstrap.py")
    mod = iu.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class TestInstallSeedEntrypoint:
    """容器首次启动走的是 `_install_seed`，不是 fixture 里那条捷径。

    2026-10-25 实测：`_install_seed` 只拷 `runtime/`，把 `seed.json` 与
    `conflict/` 丢在门外 ⇒ reset 回 `restored=True` 但 `verified=False`、
    矩阵永远全 synced（E-06 的 CONFLICT 剧本无声消失）。fixture 直接把 seed
    build 进 `.skillos-seed`，绕过了这一步，所以老测试一条都没发现。
    """

    def test_carries_manifest_and_conflict_overlay(self, tmp_path: Path) -> None:
        seed_src = tmp_path / "seed"
        _load_seed_script().build(seed_src, list(_RUNTIMES))
        root = tmp_path / "root"
        root.mkdir()

        _load_bootstrap_script()._install_seed(root, seed_src)

        dst = root / ".skillos-seed"
        assert (dst / "seed.json").is_file()
        assert (dst / "conflict").is_dir()
        assert (dst / "runtime" / _RUNTIMES[0]).is_dir()
        # 自证有据可依（否则 reset 只会报 "seed.json 缺失，跳过自证"）
        assert _make_engine(root)._seed_manifest() is not None
