"""P7 沙盒（FR-2.4.3 可写隔离 / FR-3.10 重置接口）+ doctor 预案项。

沙盒三件事（04 §2.7.7）：
1. `SKILLOS_ROOT`：入口层把 home 指向容器内虚拟目录（T1：root 由入口显式传入，
   core/fs 无感知、不读环境变量）
2. `sandbox_reset`：恢复预置演示数据（评委可反复体验的前提）
3. 前端不隐藏写操作 —— 隔离靠文件系统边界，不靠 UI 禁用

reset 安全语义：
- 仅沙盒模式可达（engine.is_sandbox / daemon 分支 / API 三层检查）
- seed 不存在拒绝执行（reset ≠ 清空）
- **runtime root 必须在 home（容器根）之内** —— 越界报错不删（红线 16：
  判断不了就报错，不许猜；此守卫删除后 test_reset_refuses_runtime_outside_home 必红）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillos.api.main import create_app
from skillos.core.model import DriftState
from skillos.services.engine import EnvPaths, LocalEngine, resolve_home

_SKILL_MD = (
    b"---\nname: demo\nversion: 1.0.0\n"
    b"description: a demo skill doing useful everyday work\n---\nbody\n"
)
_SKILL_MD2 = (
    b"---\nname: extra\nversion: 1.0.0\n"
    b"description: an extra skill for the sandbox seed set\n---\nbody\n"
)


def _make_engine(home: Path, *, sandbox: bool) -> LocalEngine:
    paths = EnvPaths(
        repo_root=home / ".skillos" / "repo",
        db_path=home / ".skillos" / "skillos.db",
    )
    return LocalEngine(paths, home=home, sandbox=sandbox)


@pytest.fixture()
def sandbox(tmp_path: Path):
    """沙盒环境：home = 容器根（tmp），seed 预置 2 个 skill（claude + agents 两端）。

    首次启动 = 容器 entrypoint 语义：seed → runtime 目录，再 init / import / push。
    """
    import shutil

    home = tmp_path / "root"
    seed = home / ".skillos-seed" / "runtime"
    for rt in ("claude", "agents"):
        for slug, md in (("demo", _SKILL_MD), ("extra", _SKILL_MD2)):
            d = seed / rt / slug
            d.mkdir(parents=True)
            (d / "SKILL.md").write_bytes(md)
        # 首次启动：seed 复制到 runtime deploy 根
        shutil.copytree(seed / rt, home / f".{rt}" / "skills", dirs_exist_ok=True)
    engine = _make_engine(home, sandbox=True)
    engine.init_env()
    engine.import_skills()
    engine.push_skill("demo")
    engine.push_skill("extra")
    return engine, home


class TestResolveHome:
    """入口层 home 解析（T1）：SKILLOS_ROOT 存在时指向容器根。"""

    def test_env_overrides_home(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("SKILLOS_ROOT", str(tmp_path / "root"))
        assert resolve_home() == tmp_path / "root"

    def test_default_is_home(self, monkeypatch) -> None:
        monkeypatch.delenv("SKILLOS_ROOT", raising=False)
        assert resolve_home() == Path.home()


class TestSandboxReset:
    """engine.sandbox_reset（FR-3.10）。"""

    def test_reset_requires_sandbox(self, tmp_path) -> None:
        engine = _make_engine(tmp_path / "root", sandbox=False)
        engine.init_env()
        with pytest.raises(ValueError, match="NOT_SANDBOX"):
            engine.sandbox_reset()

    def test_reset_requires_seed(self, tmp_path) -> None:
        engine = _make_engine(tmp_path / "root", sandbox=True)
        engine.init_env()
        # seed 不存在 → 拒绝执行（reset ≠ 清空）
        with pytest.raises(ValueError, match="SEED_MISSING"):
            engine.sandbox_reset()

    def test_reset_refuses_runtime_outside_home(self, sandbox) -> None:
        """红线 16 变异验证：runtime root 越出容器根 → 报错、绝不删任何内容。
        把守卫删掉本测试必红（outside 目录会被清空）。"""
        engine, home = sandbox
        outside = home.parent / "outside-skills"
        outside.mkdir()
        (outside / "precious").mkdir()
        (outside / "precious" / "SKILL.md").write_bytes(b"do not delete\n")
        # 把 claude 的 runtime root 篡改到容器外
        rt = next(r for r in engine.db.list_runtimes() if r.key == "claude")
        engine.db.upsert_runtime(rt.id, rt.key, rt.adapter_key, outside, rt.scope)
        with pytest.raises(ValueError, match="BOUNDARY"):
            engine.sandbox_reset()
        # 越界目录原封不动
        assert (outside / "precious" / "SKILL.md").read_bytes() == b"do not delete\n"

    def test_reset_restores_seed_state(self, sandbox) -> None:
        """制造全面漂移（runtime 手改 + skill 删除 + canonical 前进 + 未纳管目录）
        → reset 后恢复出厂状态且全部 SYNCED。

        出厂态口径：push 后的形态（render_layout 按字段策略重建 SKILL.md，
        如 claude adapter 丢弃 version 字段——seed 只是源材料，部署字节
        是 canonical 的渲染产物，与产品正常 push 行为一致）。
        """
        import shutil as _shutil

        engine, home = sandbox
        # 出厂期望字节：fixture push 后的 runtime 实况（reset 重演同一路径）。
        # 各端渲染产物不同（agents 保留 version、claude 丢弃）→ 按 runtime 采样
        expected = {
            (rt, slug): (home / f".{rt}" / "skills" / slug / "SKILL.md").read_bytes()
            for rt in ("claude", "agents")
            for slug in ("demo", "extra")
        }
        # 漂移 1：runtime 手改 demo
        (home / ".claude" / "skills" / "demo" / "SKILL.md").write_bytes(b"tampered")
        # 漂移 2：runtime 删掉 extra
        for rt in ("claude", "agents"):
            _shutil.rmtree(home / f".{rt}" / "skills" / "extra")
        # 漂移 3：canonical 前进
        tree = engine.file_tree("demo")
        base = next(f["hash"] for f in tree if f["path"] == "SKILL.md")
        engine.write_file("demo", "SKILL.md", _SKILL_MD + b"v2\n",
                          base_hash=base, message="drift")
        # 漂移 4：runtime 出现未纳管目录
        junk = home / ".claude" / "skills" / "junk"
        junk.mkdir()
        (junk / "SKILL.md").write_bytes(b"junk")

        result = engine.sandbox_reset()
        assert result["restored"] is True
        assert sorted(result["skills"]) == ["demo", "extra"]

        # runtime 字节 == 出厂实况（push 后形态，按端比对）
        for (rt, slug), content in expected.items():
            assert (home / f".{rt}" / "skills" / slug / "SKILL.md").read_bytes() == content
        # 未纳管 junk 已清除（恢复出厂语义）
        assert not junk.exists()
        # 索引恢复：2 个 skill、矩阵全 SYNCED
        from skillos.store.index_repo import SkillQuery

        slugs = {r.slug for r in engine.db.list_skills(SkillQuery(limit=10000))}
        assert slugs == {"demo", "extra"}
        assert all(c.state == DriftState.SYNCED for c in engine.status_matrix())
        # canonical 已重建（reset 后历史即重建证据；reset commit 溯源语义）
        commits = engine.git.log(path="skills/demo", limit=10)
        assert any("skillos" in c.message for c in commits)

    def test_reset_idempotent(self, sandbox) -> None:
        from skillos.store.index_repo import SkillQuery

        engine, _ = sandbox
        assert engine.sandbox_reset()["restored"] is True
        assert engine.sandbox_reset()["restored"] is True
        slugs = {r.slug for r in engine.db.list_skills(SkillQuery(limit=10000))}
        assert slugs == {"demo", "extra"}


class TestTaskExecutorSandbox:
    """sandbox_reset 经白名单执行器（WS 通道同路径）。"""

    def test_routes_to_engine(self, sandbox) -> None:
        from skillos.daemon.main import TaskExecutor

        engine, _ = sandbox
        r = TaskExecutor(engine).execute("sandbox_reset", {})
        assert r["ok"] is True
        assert r["data"]["restored"] is True

    def test_non_sandbox_refused(self, tmp_path) -> None:
        from skillos.daemon.main import TaskExecutor

        engine = _make_engine(tmp_path / "root", sandbox=False)
        engine.init_env()
        r = TaskExecutor(engine).execute("sandbox_reset", {})
        assert r["ok"] is False
        assert r["error"]["code"] == "NOT_SANDBOX"


class TestSandboxApi:
    """API 层（FR-3.10）：仅 SKILLOS_SANDBOX=1 时存在；health 暴露沙盒标志。"""

    @pytest.fixture()
    def app_client(self, tmp_path):
        app = create_app(
            db_url=f"sqlite:///{tmp_path / 'cloud.db'}",
            jwt_secret="test-secret",
            sandbox=True,
        )
        with TestClient(app) as c:
            yield c

    def test_health_reports_sandbox(self, app_client) -> None:
        r = app_client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["data"]["sandbox"] is True

    def test_reset_dispatches_to_daemon(self, app_client) -> None:
        """沙盒模式：reset 经 hub 下发；daemon 离线 → 503（同 push 语义）。"""
        token = _register_and_login(app_client)
        r = app_client.post("/api/sandbox/reset",
                            headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 503
        assert r.json()["detail"] == "DAEMON_OFFLINE"

    def test_reset_404_when_not_sandbox(self, tmp_path) -> None:
        app = create_app(
            db_url=f"sqlite:///{tmp_path / 'cloud.db'}",
            jwt_secret="test-secret",
            sandbox=False,
        )
        with TestClient(app) as c:
            token = _register_and_login(c)
            r = c.post("/api/sandbox/reset",
                       headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 404


def _register_and_login(client: TestClient) -> str:
    r = client.post("/api/auth/register", json={
        "email": "u@example.com", "password": "password123", "name": "u",
    })
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", json={
        "email": "u@example.com", "password": "password123",
    })
    assert r.status_code == 200
    return r.json()["data"]["token"]


class TestDoctorProvenanceIdentity:
    """doctor 第 10 项（防砸预案 10）：git user.name 未配置 → 时间轴作者显示
    unknown，收尾叙事失去实物背书。doctor 必须报 warn。

    测试用 GIT_CONFIG_GLOBAL/SYSTEM 指向空文件，隔离本机全局配置
    （本机全局可能配了 user.name，会污染「未配置」判定）。
    """

    @staticmethod
    def _isolate_git_global(tmp_path: Path, monkeypatch) -> None:
        empty = tmp_path / "git-config-empty"
        empty.write_text("", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))

    def test_warns_when_git_identity_missing(self, tmp_path, monkeypatch) -> None:
        from rich.console import Console

        from skillos.cli.doctor import run_doctor

        home = tmp_path / "root"
        monkeypatch.setenv("SKILLOS_ROOT", str(home))
        self._isolate_git_global(tmp_path, monkeypatch)
        engine = _make_engine(home, sandbox=False)
        engine.init_env()
        console = Console(file=open(tmp_path / "out.txt", "w", encoding="utf-8"))
        run_doctor(console)
        out = Path(tmp_path / "out.txt").read_text(encoding="utf-8")
        seg = out.split("溯源身份")[1][:60]
        assert "未配置" in seg

    def test_ok_when_git_identity_set(self, tmp_path, monkeypatch) -> None:
        import subprocess

        from rich.console import Console

        from skillos.cli.doctor import run_doctor

        home = tmp_path / "root"
        monkeypatch.setenv("SKILLOS_ROOT", str(home))
        self._isolate_git_global(tmp_path, monkeypatch)
        engine = _make_engine(home, sandbox=False)
        engine.init_env()
        subprocess.run(
            ["git", "-C", str(home / ".skillos" / "repo"),
             "config", "user.name", "demo-user"],
            capture_output=True, check=True,
        )
        console = Console(file=open(tmp_path / "out.txt", "w", encoding="utf-8"))
        run_doctor(console)
        out = Path(tmp_path / "out.txt").read_text(encoding="utf-8")
        # repo 级配置优先 → 报 ok（user.name= 详见情）
        seg = out.split("溯源身份")[1][:60]
        assert "user.name=demo-user" in seg
        assert "未配置" not in seg
