"""MCP Server 八工具（FR-1.3.x / P5）。

验证口径：
- 工具集白名单（八工具，无通用 git / shell —— OUT-14）
- 写入类默认 dry_run=true（FR-1.3.3）；显式 dry_run=false 才落盘
- 无 force / on_conflict 参数（FR-1.3.5）
- GATE_FAILED 原样返回、无绕过开关（FR-1.3.6）
- sync_pull：dry_run 与真跑同一判据（FR-1.3.7）；双向分歧 → CONFLICT
- upgrade 显式 NOT_IMPLEMENTED（红线 7：不静默成功）

工具函数经 asyncio.run 调用（异步入口 + to_thread 下沉，与真实请求路径一致）；
stdio 传输层由 scripts/smoke_mcp_stdio.py 冒烟覆盖。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from skillos.mcp import server as mcp_server
from skillos.services.engine import LocalEngine, default_env

_SKILL_MD = (
    b"---\nname: demo\nversion: 1.0.0\n"
    b"description: a demo skill doing useful everyday work\n---\nbody\n"
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def mcp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    d = home / ".claude" / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(_SKILL_MD)
    monkeypatch.setenv("SKILLOS_HOME", str(home))
    mcp_server._engine = None  # 重置单例
    engine = LocalEngine(default_env(home), home=home)
    engine.init_env()
    engine.import_skills()
    yield engine, home
    mcp_server._engine = None


def test_tool_whitelist():
    """八工具且只有八工具（OUT-14：无 git / shell / exec 类通用工具）。"""
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = sorted(t.name for t in tools)
    assert names == sorted([
        "skillos_list_skills", "skillos_get_skill", "skillos_status",
        "skillos_check", "skillos_version_log", "skillos_sync_push",
        "skillos_sync_pull", "skillos_upgrade",
    ])
    # 无 force / on_conflict / replace_policy 参数（FR-1.3.5 / 04 §4.3 约束 6）
    push = next(t for t in tools if t.name == "skillos_sync_push")
    props = push.inputSchema.get("properties", {})
    assert "force" not in props and "on_conflict" not in props
    assert "replace_policy" not in props
    assert props["dry_run"]["default"] is True  # FR-1.3.3
    # 全部工具为 async（阻塞实现已下沉 worker 线程，见 server.py 工程要点）
    for name in names:
        fn = getattr(mcp_server, name)
        assert asyncio.iscoroutinefunction(fn), name


def test_read_tools(mcp_env):
    r = run(mcp_server.skillos_list_skills())
    assert r["ok"] and r["data"]["skills"][0]["slug"] == "demo"

    r = run(mcp_server.skillos_get_skill("demo"))
    assert r["ok"]
    assert "files" in r["data"] and "skill_md" in r["data"]

    r = run(mcp_server.skillos_status())
    assert r["ok"] and len(r["data"]["cells"]) >= 1

    r = run(mcp_server.skillos_check("demo"))
    assert r["ok"] and isinstance(r["data"]["findings"], list)

    r = run(mcp_server.skillos_version_log("demo", limit=5))
    assert r["ok"] and len(r["data"]["items"]) >= 1


def test_push_default_dry_run_writes_nothing(mcp_env):
    """FR-1.3.3：默认 dry_run，不落盘。"""
    r = run(mcp_server.skillos_sync_push("demo"))  # dry_run 缺省 true
    assert r["ok"] and r["dry_run"] is True
    # 四端目标目录均未写入（未落盘）：除 claude 源目录外无 demo 目录
    home = mcp_env[1]
    for key in ("agents", "workbuddy", "codex"):
        assert not (home / f".{key}" / "skills" / "demo").exists(), key
    # Agent 侧判据：wrote=False 不等于「无需同步」，必须同时看 action/would_write
    for o in r["data"]["outcomes"]:
        assert o["wrote"] is False
        assert o["action"] in ("skip", "write")
        assert o["would_write"] is (o["action"] == "write")


def test_push_real_then_gate_failed_passthrough(mcp_env):
    """真跑成功；门禁 error 的 skill → GATE_FAILED 原样返回（FR-1.3.6）。"""
    engine, home = mcp_env
    r = run(mcp_server.skillos_sync_push("demo", dry_run=False))
    assert r["ok"] and all(o["ok"] for o in r["data"]["outcomes"])

    # 破坏门禁：name 为空（fm.name.required = ERROR）
    engine.write_file(
        "demo", "SKILL.md",
        _SKILL_MD.replace(b"name: demo", b"name: \"\""),
        base_hash=hashlib.sha256(_SKILL_MD).hexdigest(),
        message="break gate",
    )
    r = run(mcp_server.skillos_sync_push("demo"))
    assert not r["ok"]
    codes = [o["error"]["code"] for o in r["data"]["outcomes"] if not o["ok"]]
    assert codes == ["GATE_FAILED"]
    # 明细可见（人可读判据，NFR-10.1）
    assert "fm.name.required" in str(r["data"]["outcomes"])


def test_sync_pull_dry_run_matches_real(mcp_env):
    """FR-1.3.7：dry_run 预览与真跑结论一致（同一判据）。"""
    engine, home = mcp_env
    run(mcp_server.skillos_sync_push("demo", dry_run=False))
    # 手改 claude 磁盘
    disk = home / ".claude" / "skills" / "demo" / "SKILL.md"
    disk.write_bytes(_SKILL_MD.replace(b"body\n", b"body edited\n"))

    preview = run(mcp_server.skillos_sync_pull("demo", "claude"))  # 默认 dry_run
    assert preview["ok"] and preview["dry_run"] is True
    assert preview["changed"] == ["SKILL.md"]

    real = run(mcp_server.skillos_sync_pull("demo", "claude", dry_run=False))
    assert real["ok"] and real["changed"] == ["SKILL.md"]
    assert "commit" in real
    # 回收后 canonical 即磁盘内容
    snap = engine.git.snapshot_at(engine.head(), "demo")
    assert snap.files[0].content == disk.read_bytes()
    # binding 收敛 synced
    row = engine.db.get_skill_by_slug("demo")
    b = engine.db.get_binding(row.id, "claude")
    assert b.status == "synced" and b.installed_commit == real["commit"]


def test_sync_pull_conflict_on_canonical_advance(mcp_env):
    """双向分歧（canonical 已前进）→ CONFLICT，不落盘（FR-1.3.5）。"""
    engine, home = mcp_env
    run(mcp_server.skillos_sync_push("demo", dry_run=False))
    # 磁盘改
    (home / ".claude" / "skills" / "demo" / "SKILL.md").write_bytes(
        _SKILL_MD.replace(b"body\n", b"disk\n"))
    # canonical 也前进（web 编辑）
    tree = engine.file_tree("demo")
    base = next(f["hash"] for f in tree if f["path"] == "SKILL.md")
    engine.write_file("demo", "SKILL.md", _SKILL_MD.replace(b"body\n", b"canon\n"),
                      base_hash=base, message="web edit")
    r = run(mcp_server.skillos_sync_pull("demo", "claude"))
    assert not r["ok"] and r["error_code"] == "CONFLICT"


def test_upgrade_not_implemented(mcp_env):
    """上游源属 P7：显式 NOT_IMPLEMENTED（红线 7）。"""
    r = run(mcp_server.skillos_upgrade("demo"))
    assert not r["ok"] and r["error"]["code"] == "NOT_IMPLEMENTED"


def test_invalid_inputs_structured(mcp_env):
    """错误不外抛：结构化 INVALID_INPUT（FR-3.19）。"""
    assert run(mcp_server.skillos_get_skill("ghost"))["error"]["code"] == "INVALID_INPUT"
    assert run(mcp_server.skillos_check("ghost"))["error"]["code"] == "INVALID_INPUT"
    r = run(mcp_server.skillos_sync_pull("demo", "no-such-runtime"))
    assert r["error"]["code"] == "INVALID_INPUT"
