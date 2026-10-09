"""mcp —— MCP Server 入口（FR-1.3.x，stdio 传输）。

工具清单（4.3，白名单）：
  skillos_list_skills / skillos_get_skill / skillos_status / skillos_check /
  skillos_version_log / skillos_sync_push / skillos_sync_pull / skillos_upgrade

约束（docs/04 §4.3，红线）：
1. 写入类工具默认 dry_run=true（FR-1.3.3），需显传 dry_run=false 才落盘。
2. 不暴露 force（FR-1.3.5）：无 on_conflict 参数，冲突只能 abort / skip。
3. 不暴露通用 git / shell（OUT-14）：只有领域操作。
4. 无设备管理工具：token 撤销只在 Web 人工路径。
5. 无门禁绕过开关（FR-1.3.6）：GATE_FAILED 原样返回，由人在 Web 决定。
6. dry_run 与真跑共用同一代码路径（FR-1.3.7）——引擎层保证，此处不另写预览逻辑。

MCP 是与 CLI / daemon 平行的第三入口：直接调 services 层（LocalEngine），
不经过云端（本地操作）也不抢 daemon 的 WS 通道。

工程要点：SDK 对同步工具**直接在事件循环线程执行**；引擎的 git 子进程
（subprocess.communicate）在该线程会与 anyio stdio 传输死锁（Windows 实测）。
因此所有工具一律 async + anyio.to_thread.run_sync 下沉 worker 线程——
这也符合「阻塞调用不占事件循环」的常规约束。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP

from skillos.core.logging import (
    bind_op_id,
    get_logger,
    reset_op_context,
    setup_logging,
)
from skillos.services.engine import LocalEngine, default_env

mcp = FastMCP("skillos", instructions=(
    "Skill OS —— 个人 AI Skill 操作系统。写入类工具默认 dry_run=true，"
    "先预览（dry_run）确认无误后再显式传 dry_run=false 落盘。"
    "冲突与删除确认没有 force 通道，需人工在 Web 处理。"
))

log = get_logger("skillos.mcp")

_engine: LocalEngine | None = None


def get_engine() -> LocalEngine:
    """进程内单例。SKILLOS_HOME 供沙盒/测试重定向（T1：路径显式）。"""
    global _engine
    if _engine is None:
        home = Path(os.environ.get("SKILLOS_HOME") or Path.home())
        _engine = LocalEngine(default_env(home), home=home)
    return _engine


def _err(code: str, message: str) -> dict:
    """结构化错误（FR-3.19 同源码表）：工具不抛异常，Agent 可读码自救。"""
    return {"ok": False, "error": {"code": code, "message": message}}


def _threaded(fn: Callable[[], dict]) -> Any:
    """把阻塞实现下沉 worker 线程（见模块 docstring 工程要点）。

    同时为本次调用生成 op_id（每次 MCP request 一个），
    structlog contextvars 在 worker 线程中正确传递。
    """
    bind_op_id(component="mcp", tool=getattr(fn, "__name__", "tool"))
    try:
        return anyio.to_thread.run_sync(fn)
    finally:
        reset_op_context()


# ---------------------------------------------------------------- 只读工具


@mcp.tool()
async def skillos_list_skills() -> dict:
    """列出库内全部 Skill：slug / 聚合漂移态 / 各 runtime 绑定态。"""
    return await _threaded(_list_skills_impl)


def _list_skills_impl() -> dict:
    engine = get_engine()
    agg = engine.aggregate_status()
    by_slug: dict[str, dict] = {}
    for cell in engine.status_matrix():
        by_slug.setdefault(cell.skill_id, {"cells": {}})
        by_slug[cell.skill_id]["cells"][cell.runtime_key] = (
            cell.state.name if hasattr(cell.state, "name") else str(cell.state)
        )
    from skillos.store.index_repo import SkillQuery

    rows = engine.db.list_skills(SkillQuery(limit=10000))
    skills = []
    for r in rows:
        skills.append({
            "slug": r.slug,
            "name": r.name,
            "state": agg.get(r.slug).name if agg.get(r.slug) else None,
            "runtimes": by_slug.get(r.id, {}).get("cells", {}),
        })
    return {"ok": True, "data": {"skills": skills}}


@mcp.tool()
async def skillos_get_skill(slug: str) -> dict:
    """取单个 Skill：元数据 + 文件树 + SKILL.md 正文（FR-3.12：内容实时读 canonical）。"""
    return await _threaded(lambda: _get_skill_impl(slug))


def _get_skill_impl(slug: str) -> dict:
    engine = get_engine()
    if engine.db.get_skill_by_slug(slug) is None:
        return _err("INVALID_INPUT", f"未纳管的 skill: {slug}")
    try:
        tree = engine.file_tree(slug)
        snapshot = engine.git.snapshot_at(engine.head(), slug)
    except Exception as e:  # noqa: BLE001 —— 快照失败统一转 INVALID_INPUT
        return _err("INVALID_INPUT", str(e))
    skill_md = next((f.content for f in snapshot.files if f.path == "SKILL.md"), b"")
    return {"ok": True, "data": {
        "slug": slug,
        "meta": {
            "name": snapshot.meta.name,
            "description": snapshot.meta.description,
            "version": snapshot.meta.version,
        },
        "files": tree,
        "skill_md": skill_md.decode("utf-8", errors="replace"),
    }}


@mcp.tool()
async def skillos_status(slug: str | None = None) -> dict:
    """漂移矩阵。slug 省略 = 全部；给出 = 该 Skill 的逐 runtime 状态。"""
    return await _threaded(lambda: _status_impl(slug))


def _status_impl(slug: str | None) -> dict:
    engine = get_engine()
    cells = engine.status_matrix()
    if slug:
        row = engine.db.get_skill_by_slug(slug)
        if row is None:
            return _err("INVALID_INPUT", f"未纳管的 skill: {slug}")
        cells = [c for c in cells if c.skill_id == row.id]
    return {"ok": True, "data": {"cells": [
        {"skill_id": c.skill_id, "runtime_key": c.runtime_key,
         "state": c.state.name if hasattr(c.state, "name") else str(c.state)}
        for c in cells
    ]}}


@mcp.tool()
async def skillos_check(slug: str) -> dict:
    """质量门禁（FR-1.0.11 / NFR-10.5：与 push 前置校验同一份判定）。"""
    return await _threaded(lambda: _check_impl(slug))


def _check_impl(slug: str) -> dict:
    engine = get_engine()
    if engine.db.get_skill_by_slug(slug) is None:
        return _err("INVALID_INPUT", f"未纳管的 skill: {slug}")
    try:
        result = engine.gate_of(slug)
    except Exception as e:  # noqa: BLE001
        return _err("INVALID_INPUT", str(e))
    return {"ok": True, "data": {
        "has_error": result.has_error,
        "findings": [
            {"rule": f.rule, "level": str(f.level), "message": f.message}
            for f in result.findings
        ],
    }}


@mcp.tool()
async def skillos_version_log(slug: str, limit: int = 20) -> dict:
    """版本历史（git log，FR-2.2.7 溯源时间轴同源数据）。"""
    return await _threaded(lambda: _version_log_impl(slug, limit))


def _version_log_impl(slug: str, limit: int) -> dict:
    engine = get_engine()
    commits = engine.git.log(path=f"skills/{slug}", limit=max(1, min(limit, 200)))
    return {"ok": True, "data": {"items": [
        {"sha": c.sha, "at": c.at, "message": c.message} for c in commits
    ]}}


# ---------------------------------------------------------------- 写入工具（默认 dry_run）


@mcp.tool()
async def skillos_sync_push(slug: str, targets: list[str] | None = None,
                            dry_run: bool = True) -> dict:
    """分发 canonical → 各 Runtime（默认 dry_run=true 只预览，FR-1.3.3）。

    门禁 error 时返回 GATE_FAILED 与规则明细（FR-1.3.6：无绕过开关）。
    无 on_conflict / force 参数（FR-1.3.5）：冲突与删除确认走 Web 人工路径。
    """
    return await _threaded(lambda: _push_impl(slug, targets, dry_run))


def _push_impl(slug: str, targets: list[str] | None, dry_run: bool) -> dict:
    engine = get_engine()
    log.info(
        "sync_push_started",
        skill_slug=slug,
        targets=targets, dry_run=dry_run,
    )
    try:
        report = engine.push_skill(
            slug,
            runtime_keys=[str(t) for t in targets] if targets else None,
            dry_run=dry_run,
            allow_gate_failure=False,  # FR-1.3.6：MCP 路径无开关
            confirmed_removals=None,   # 无人确认 → 待删文件 held_back
        )
    except ValueError as e:
        log.warning(
            "sync_push_failed",
            skill_slug=slug,
            error_code="INVALID_INPUT", detail=str(e),
        )
        return _err("INVALID_INPUT", str(e))
    outcomes = []
    held_back: list[str] = []
    for o in report.outcomes:
        item = {"runtime": o.runtime_key, "ok": o.ok, "wrote": o.wrote}
        if o.error_code:
            item["error"] = {"code": o.error_code, "message": o.detail or ""}
            item["held_back_removals"] = list(o.held_back_removals or [])
            held_back.extend(o.held_back_removals or [])
        if o.new_installed_hash:
            item["installed_hash"] = o.new_installed_hash
        outcomes.append(item)
    if dry_run:
        log.info(
            "dry_run_executed",
            op="sync_push", skill_slug=slug,
            held_count=len(held_back),
        )
    elif report.ok:
        log.info(
            "sync_push_completed",
            skill_slug=slug, targets=targets,
        )
    else:
        log.warning(
            "sync_push_failed",
            skill_slug=slug,
            error_code=next((o.error_code for o in report.outcomes if o.error_code), None),
        )
    return {
        "ok": report.ok, "dry_run": dry_run,
        "data": {"outcomes": outcomes, "held_back_removals": sorted(set(held_back))},
    }


@mcp.tool()
async def skillos_sync_pull(slug: str, runtime: str, dry_run: bool = True) -> dict:
    """从 Runtime 磁盘回收改动为 canonical 新版本（默认 dry_run=true，FR-1.3.3）。

    双向分歧（canonical 也前进过）→ CONFLICT，交人工在 Web 处理（FR-1.3.5）。
    预览与真跑共用同一判据（FR-1.3.7）。
    """
    return await _threaded(lambda: _pull_impl(slug, runtime, dry_run))


def _pull_impl(slug: str, runtime: str, dry_run: bool) -> dict:
    engine = get_engine()
    log.info(
        "sync_pull_started",
        skill_slug=slug, runtime_key=runtime, dry_run=dry_run,
    )
    try:
        result = engine.pull_skill(slug, runtime, dry_run=dry_run)
    except ValueError as e:
        log.warning(
            "sync_pull_failed",
            skill_slug=slug, runtime_key=runtime,
            error_code="INVALID_INPUT", detail=str(e),
        )
        return _err("INVALID_INPUT", str(e))
    if dry_run:
        log.info(
            "dry_run_executed",
            op="sync_pull", skill_slug=slug, runtime_key=runtime,
        )
    elif result.get("ok"):
        log.info(
            "sync_pull_completed",
            skill_slug=slug, runtime_key=runtime,
        )
    return result


@mcp.tool()
async def skillos_upgrade(slug: str, strategy: str = "merge", dry_run: bool = True) -> dict:
    """从上游源升级（FR-1.0.13，依赖 upstream_sources——P7 接入）。

    当前版本：上游源管理未交付，显式返回 NOT_IMPLEMENTED（红线 7：不静默成功）。
    """
    return await _threaded(lambda: _upgrade_impl(slug))


def _upgrade_impl(slug: str) -> dict:
    return _err("NOT_IMPLEMENTED",
                "上游源（upstream）属 P7 范围；当前请用 skillos_version_log 人工比对")


def main() -> None:
    """MCP Server 入口（FR-1.3.x，stdio 传输）。

    关键：stdio 模式**只走 stderr 不落文件**（04 §6.2.4 Sink 表 MCP 行）；
    setup_logging("mcp") 不创建 ~/.skillos/logs/mcp.log，
    只挂 stderr handler（T23 实现约束）。
    """
    setup_logging("mcp")
    bind_op_id(component="mcp_server")
    log.info(
        "mcp_session_opened",
    )
    try:
        mcp.run()  # stdio 传输（02 §9：官方 SDK）
    finally:
        log.info(
            "mcp_session_closed",
        )


if __name__ == "__main__":
    main()
