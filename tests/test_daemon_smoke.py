"""Daemon 冒烟测试（P3 升级）：每个新 op 走真 dispatch 路径.

覆盖之前 daemon ImportError 漏的代码路径（discover 同名冲突）:
- engine.list_potential_runtimes() 内部
  `from skillos.core.algorithm.discover import discover_potential_runtimes`
  —— 懒加载，TestClient 启动不触发，真实调 `runtimes_list` op 才触发
- engine.protocol_status() 内部
  `from skillos.core.fs.protocol import CURRENT_PROTOCOL, check, ...`
- engine.diagnose_custom_path() 走 `Path.exists` / `iterdir`

守门:
- 不静默成功（07 N12 INVALID_INPUT）
- 不抛未捕获异常（FR-1.0.5 / T16 哈希 + 错误传播）

不做什么（D8 / OUT-14 红线保持）:
- 不起真 daemon 子进程（pytest 走 TaskExecutor 路径，与 test_sync_ws.py 一致）
- 不连真 WS（TestClient 不需要 daemon 实际 listen）
- 不写磁盘 / 不写 binding（只读 op）

Refs: 00 FR-1.0.32~33 / FR-1.0.29 / FR-2.4.10 / 02 T24~T26 / 04 §8.1 / 07 N12
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skillos.daemon.main import TaskExecutor
from skillos.services.engine import LocalEngine, default_env

_SKILL_MD = (
    b"---\nname: demo\nversion: 1.2.0\n"
    b"description: a demo skill doing useful work\n---\nbody\n"
)


@pytest.fixture()
def engine_home(tmp_path: Path):
    """真 LocalEngine + 临时 home：与 test_sync_ws.py engine_home 同模式."""
    home = tmp_path / "home"
    d = home / ".claude" / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(_SKILL_MD)
    engine = LocalEngine(default_env(home), home=home)
    engine.init_env()
    engine.import_skills()
    return engine, home


class TestDaemonOpsSmoke:
    """P3 升级后 3 个新 op 走真 dispatch —— 防止 lazy import 漏抓."""

    def test_runtimes_list_returns_7_candidates(self, engine_home):
        """FR-2.4.10 / T24 / FR-1.0.32: 白名单 7 个, 与 yaml 一致.

        覆盖之前 ImportError（discover.py / discover/ 同名冲突）.
        """
        engine, _ = engine_home
        r = TaskExecutor(engine).execute("runtimes_list", {})
        assert r["ok"], r
        assert "candidates" in r["data"]
        assert len(r["data"]["candidates"]) == 7
        keys = {c["key"] for c in r["data"]["candidates"]}
        assert keys == {
            "cursor", "windsurf", "cline", "continue", "roo", "aider", "trae",
        }, f"yaml 漂移: actual={keys}"

    def test_runtimes_list_has_builtins(self, engine_home):
        """FR-1.0.32: builtins 至少 4 个内置 adapter."""
        engine, _ = engine_home
        r = TaskExecutor(engine).execute("runtimes_list", {})
        assert r["ok"], r
        assert "builtins" in r["data"]
        keys = {b["key"] for b in r["data"]["builtins"]}
        assert {"claude", "agents", "workbuddy", "codex"}.issubset(keys), (
            f"内置 adapter 缺失: actual={keys}"
        )

    def test_runtime_diagnose_missing_path(self, engine_home):
        """FR-1.0.33 / D9: 不存在路径 → exists=False, 不抛异常."""
        engine, _ = engine_home
        r = TaskExecutor(engine).execute("runtime_diagnose", {
            "root_path": "/nonexistent/skillos/test/path/that/should/not/exist",
        })
        assert r["ok"], r
        assert r["data"]["exists"] is False
        assert r["data"]["skill_count"] == 0
        assert r["data"]["error"] is None

    def test_runtime_diagnose_empty_path(self, engine_home):
        """FR-1.0.33: 空路径 → 结构化 error 字段, 不抛异常."""
        engine, _ = engine_home
        r = TaskExecutor(engine).execute("runtime_diagnose", {"root_path": ""})
        assert r["ok"], r
        assert r["data"]["error"] == "path is empty"
        assert r["data"]["exists"] is False

    def test_protocol_status_returns_dict(self, engine_home):
        """FR-1.0.29 / T18: protocol_status 返回 dict 含 5 关键字段.

        关键: 即使仓库无 commit 也要 ok=True (空 repo 不是错, T18 协议协商的
        软启动要求 head_message=None 时仍返回结构化 dict).
        """
        engine, _ = engine_home
        r = TaskExecutor(engine).execute("protocol_status", {})
        assert r["ok"], r
        data = r["data"]
        for key in ("current_supported", "sticky_version", "trailer_version",
                    "ok", "read_only"):
            assert key in data, f"缺失字段: {key}"
        assert isinstance(data["ok"], bool)
        assert isinstance(data["read_only"], bool)

    def test_unknown_op_still_rejected(self, engine_home):
        """07 N12 回归守卫: 未知 op 必须结构化 INVALID_INPUT, 不静默成功.

        加新 op 后必须仍守此约定 —— 不允许 dispatch 末尾的 return 走默认
        `{"ok": True, ...}` 路径.
        """
        engine, _ = engine_home
        r = TaskExecutor(engine).execute("rm_rf", {})
        assert not r["ok"]
        assert r["error"]["code"] == "INVALID_INPUT"
        assert "未知操作" in r["error"]["message"] or "rm_rf" in r["error"]["message"]
