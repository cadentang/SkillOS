"""P3 冲突四选一（FR-2.2.6 / T22 / 排期 P3，RED LINE 12）。

约定（RED LINE 12）：
- 整文件粒度四选一：保留本地 / 保留仓库 / 两个都保留 / 暂不处理
- 任一选择前必须先建安全快照且可撤销（FR-1.0.10 同源快照能力）
- 「两个都保留」只在 Web 人工路径，MCP 工具参数中不得出现 keep_both（FR-1.3.5）
- 冲突不提供 on_conflict / force 给 CLI / MCP —— push 与 pull 路径走 Web 决策

冲突语义（drift 7 态之一）：canonical 在 installed_commit 之后又有新提交，
同时 runtime 磁盘上的纳管文件与 installed_commit@runtime 不一致。双向分歧
对应 drift.classify 的 CONFLICT 态；只 disk 改或只 canonical 进 → OUTDATED /
MODIFIED，由 pull / push 自然处理，不在本测试范围。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from skillos.services.engine import LocalEngine, default_env

_SKILL_MD = (
    b"---\nname: demo\nversion: 1.0.0\n"
    b"description: a demo skill doing useful everyday work\n---\nbody v1\n"
)


@dataclass
class _Conflict:
    """手工搭好的 CONFLICT 态：磁盘改了 + canonical 前进。"""

    engine: LocalEngine
    disk_modified: Path  # runtime disk 上的目录
    installed_commit: str
    head_commit: str  # canonical HEAD


@pytest.fixture()
def conflict(tmp_path: Path) -> _Conflict:
    """搭出 CONFLICT 场景：

    1. 在 runtime 磁盘创建 demo（v1）
    2. import 进引擎
    3. push 到 claude (建立 binding + installed_commit)
    4. 引擎编辑 SKILL.md → canonical 前进（v2）
    5. runtime 磁盘编辑 SKILL.md（runtime 视角 v_local）
    现在 disk != installed_commit@render、HEAD@render != installed_commit@render → CONFLICT。
    """
    home = tmp_path / "home"
    rt = home / ".claude" / "skills" / "demo"
    rt.mkdir(parents=True)
    (rt / "SKILL.md").write_bytes(_SKILL_MD)

    e = LocalEngine(default_env(home), home=home)
    e.init_env()
    e.import_skills()
    # 先 push 一次建立 binding（installed_commit != None），
    # 只 push 到 claude —— 其他 runtime 不应进入 fixture 干扰判断。
    push_report = e.push_skill("demo", runtime_keys=["claude"])
    assert push_report.ok
    installed = e.head()

    # canonical 前进：编辑 SKILL.md（v2）
    tree = e.file_tree("demo")
    base = next(f["hash"] for f in tree if f["path"] == "SKILL.md")
    e.write_file(
        "demo", "SKILL.md",
        _SKILL_MD.replace(b"body v1", b"body v2"),
        base_hash=base, message="edit to v2",
    )
    head = e.head()
    assert head != installed

    # 磁盘改动（runtime 视角）：改 SKILL.md 内容
    (rt / "SKILL.md").write_bytes(_SKILL_MD.replace(b"body v1", b"body local"))

    return _Conflict(
        engine=e, disk_modified=rt,
        installed_commit=installed, head_commit=head,
    )


# ============================================================ 四选一实施


def test_keep_local_pulls_disk_into_canonical(conflict: _Conflict):
    """保留本地：把磁盘当前内容回收为 canonical 新版本（同 pull_skill，但走 Web 决策）。"""
    r = conflict.engine.resolve_conflict(
        "demo", "claude", "keep_local", confirmed_removals=None,
        author="alice <alice@example.com>",
    )
    assert r["ok"]
    snap = conflict.engine.git.snapshot_at(conflict.engine.head(), "demo")
    # SKILL.md 内容应等于磁盘 body local
    assert any(
        b"body local" in f.content for f in snap.files if f.path == "SKILL.md"
    )
    # 决策前快照字段存在（dirty 时为新 commit，clean 时为 head，可回滚到此）
    assert r["snapshot"] and len(r["snapshot"]) >= 7
    # 写溯源进 sync_records（NFR-9）
    from skillos.store.models import SyncRecord
    recs = conflict.engine.db._s.query(SyncRecord).filter_by(action="resolve").all()  # type: ignore[attr-defined]
    assert len(recs) == 1
    assert recs[0].by == "alice <alice@example.com>"
    assert recs[0].detail["decision"] == "keep_local"


def test_keep_remote_overwrites_disk_with_canonical(conflict: _Conflict):
    """保留仓库：用 canonical HEAD 覆盖 runtime 磁盘（distribute 路径，policy=USER_CONFIRMED）。"""
    r = conflict.engine.resolve_conflict(
        "demo", "claude", "keep_remote", author="bob <bob@human>",
    )
    assert r["ok"]
    # 磁盘 SKILL.md 应为 canonical v2（body v2）
    disk_md = (conflict.disk_modified / "SKILL.md").read_bytes()
    assert b"body v2" in disk_md and b"body local" not in disk_md
    # 决策前快照字段存在
    assert r["snapshot"] and len(r["snapshot"]) >= 7


def test_keep_both_preserves_disk_with_conflict_suffix_and_writes_remote(conflict: _Conflict):
    """两个都保留（Web 专属，MCP 无此选项）：磁盘 → SKILL.local-conflict.md，
    canonical → SKILL.md。两份都在磁盘，互不覆盖。命名遵循 FR-2.2.6。"""
    r = conflict.engine.resolve_conflict(
        "demo", "claude", "keep_both", author="carol <carol@human>",
    )
    assert r["ok"]
    files_on_disk = {p.name for p in conflict.disk_modified.iterdir()}
    assert "SKILL.md" in files_on_disk and "SKILL.local-conflict.md" in files_on_disk
    assert b"body v2" in (conflict.disk_modified / "SKILL.md").read_bytes()
    assert b"body local" in (conflict.disk_modified / "SKILL.local-conflict.md").read_bytes()


def test_defer_keeps_state_and_records_decision(conflict: _Conflict):
    """暂不处理：不改任何内容，只标记 binding 为 deferred 并落 sync_records。"""
    # 决策前的磁盘内容
    before = (conflict.disk_modified / "SKILL.md").read_bytes()
    head_before = conflict.engine.head()

    r = conflict.engine.resolve_conflict(
        "demo", "claude", "defer", author="dave <dave@human>",
    )
    assert r["ok"] and r.get("noop") is True
    # 内容未动
    assert (conflict.disk_modified / "SKILL.md").read_bytes() == before
    # canonical 未前进（defer 不写新版本；快照是否要写？这里不写以免 commit 噪音）
    assert conflict.engine.head() == head_before
    # sync_records 落了 defer 决策
    from skillos.store.models import SyncRecord
    recs = conflict.engine.db._s.query(SyncRecord).filter_by(action="resolve").all()  # type: ignore[attr-defined]
    assert len(recs) == 1
    assert recs[0].detail["decision"] == "defer"


# ============================================================ 守卫


def test_resolve_conflict_invalid_inputs(conflict: _Conflict):
    with pytest.raises(ValueError, match="未纳管"):
        conflict.engine.resolve_conflict("ghost", "claude", "keep_local")
    with pytest.raises(ValueError, match="未知 runtime"):
        conflict.engine.resolve_conflict("demo", "no-such", "keep_local")
    with pytest.raises(ValueError, match="非法决策"):
        conflict.engine.resolve_conflict("demo", "claude", "force-push")
    # 把磁盘恢复成 installed_commit 时的渲染结果 → disk 与 base 一致、
    # canonical 已前进 → 这是「OUTDATED」而非 CONFLICT，resolve_conflict 拒绝。
    from skillos.core.algorithm.adapters.claude import ClaudeAdapter
    from skillos.core.fs.safe_path import safe_path
    claude = ClaudeAdapter()
    base_snap = conflict.engine.git.snapshot_at(conflict.installed_commit, "demo")
    base_desired = dict(claude.render_layout(base_snap))
    for rel, content in base_desired.items():
        target = safe_path(conflict.disk_modified, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    with pytest.raises(ValueError, match="非 CONFLICT"):
        conflict.engine.resolve_conflict("demo", "claude", "keep_local")


def test_resolve_conflict_unmanaged_or_missing_runtime(conflict: _Conflict):
    """runtime 上无 binding（unmanaged / 未纳管目标）不允许 resolve：先 import + push。"""
    with pytest.raises(ValueError, match="无 binding"):
        conflict.engine.resolve_conflict("demo", "agents", "keep_local")


# ============================================================ MCP 红线（FR-1.3.5）


def test_mcp_sync_pull_signature_has_no_keep_both():
    """MCP 工具签名不得暴露 keep_both 选项（RED LINE 12 / FR-1.3.5）。

    这条是安全线：把 keep_both 放在 MCP 路径意味着任何 Agent 都能让用户
    在无人监督下「全收」磁盘改动，与 keep_local 边界模糊。
    """
    import inspect

    from skillos.mcp.server import _pull_impl

    sig = inspect.signature(_pull_impl)
    params = list(sig.parameters)
    assert "keep_both" not in params
    assert "decision" not in params  # MCP 路径只允许默认 pull 行为
    # ensure MCP 源码常量里没有 keep_both
    import skillos.mcp.server as mcp_mod

    src = inspect.getsource(mcp_mod)
    assert "keep_both" not in src


def test_engine_resolve_conflict_requires_decision_enum_only():
    """决策枚举是封闭的；非法值直接 ValueError。"""
    import inspect

    from skillos.services.engine import LocalEngine

    # resolve_conflict 必须存在（实现完成）
    assert hasattr(LocalEngine, "resolve_conflict")
    sig = inspect.signature(LocalEngine.resolve_conflict)
    decision_param = sig.parameters["decision"]
    # 参数类型注解必须是字面字符串 / ConflictDecision 枚举
    ann = str(decision_param.annotation)
    assert "str" in ann or "Literal" in ann or "ConflictDecision" in ann


# ============================================================ daemon 任务层


class TestTaskExecutorResolveConflict:
    """resolve_conflict 经白名单执行器：WS 通道同路径，WRONG / 非白名单 / 缺参守卫。"""

    def test_resolve_conflict_routes_to_engine(self, conflict: _Conflict):
        from skillos.daemon.main import TaskExecutor

        ex = TaskExecutor(conflict.engine)
        r = ex.execute("resolve_conflict", {
            "slug": "demo", "runtime_key": "claude", "decision": "defer",
            "author": "tester <tester@example.com>",
        })
        assert r["ok"] is True
        assert r["data"]["decision"] == "defer"

    def test_resolve_conflict_invalid_decision_returns_invalid_input(self, conflict: _Conflict):
        from skillos.daemon.main import TaskExecutor

        ex = TaskExecutor(conflict.engine)
        r = ex.execute("resolve_conflict", {
            "slug": "demo", "runtime_key": "claude", "decision": "force",
        })
        assert r["ok"] is False
        assert r["error"]["code"] == "INVALID_INPUT"

    def test_resolve_conflict_not_in_whitelist(self, conflict: _Conflict):
        from skillos.daemon.main import TaskExecutor

        ex = TaskExecutor(conflict.engine)
        r = ex.execute("resolve_conflict_auto", {"slug": "demo"})
        assert r["ok"] is False
        assert r["error"]["code"] == "INVALID_INPUT"
        assert "非白名单" in r["error"]["message"]