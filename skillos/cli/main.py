"""CLI 入口层（FR-1.1.x，04 §2.5.1 / §4.4）。

- 命令集即白名单（OUT-14）：versions / diff / rollback 是三个具名命令，
  不是 skillos git <args>；daemon WS 任务表与之一一对应。
- 退出码：0 成功 / 1 业务失败 / 2 参数错误 / 3 冲突待处理。
- CLI 与 MCP 完全不走云端（FR-2.4.3 / NFR-1），离线可用。
- 分层红线：本层只做参数解析与呈现，业务全部走 services.engine。

日志（T23）：CLI 模式默认级别 WARNING —— 用户输出走 console.print，
不归 logger；脚本串联时可设 `SKILLOS_LOG_LEVEL=INFO` 激活命令级 INFO 日志；
每次命令执行一个 op_id；--json 输出带 `op_id` 字段（04 §6.2.4 Sink 表 1 行）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from skillos.core.logging import (
    bind_op_id,
    get_logger,
    setup_logging,
)
from skillos.services.usage_service import VIA_CLI

# 05 P0-2：中文 + Windows 控制台默认 cp936，输出中文 skill 名必抛 UnicodeEncodeError。
# 入口处强制 UTF-8，必须先于任何输出执行。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

# CLI 日志初始化**不在模块级执行**（见 _cli_callback 内注释）
log = get_logger("skillos.cli")

app = typer.Typer(
    name="skillos",
    help="Skill OS —— 个人 AI Skill 操作系统（canonical Git 仓库 + 四 Runtime 分发）",
    no_args_is_help=True,
)
console = Console()


# 服务型子命令：长驻进程，自带 setup_logging（daemon / mcp / serve 各自的
# 进程入口挂自己的 sink：serve 走 `create_app()` 里的 setup_logging("api")）。
# 若 CLI 先跑 setup_logging("cli")，core.logging 的
# _initialized 守卫会把它们的初始化整个吞掉 —— daemon.log 永不创建，
# Web「最近同步记录」永远为空（2026-10-11 踩坑，详见 docs/09 排查表）。
_SERVICE_SUBCOMMANDS = frozenset({"daemon", "mcp", "serve"})


@app.callback()
def _cli_callback(ctx: typer.Context) -> None:
    """每个 CLI 命令入口：CLI 日志初始化 + 分配 op_id + 记 started（命令级事件）。

    命令完成（completed / failed）由 services 层埋点承担（避免双重记录）。
    CLI 模式默认级别 WARNING，本日志静默；`SKILLOS_LOG_LEVEL=INFO` 激活。
    """
    cmd = ctx.invoked_subcommand or "skillos"
    # 必须在命令执行前（而非 import 时）初始化，且跳过服务型子命令：
    # 它们的入口（daemon/main.py / mcp/server.py）各自调 setup_logging
    # 挂自己的 sink，被 cli 初始化抢先会让 _initialized 守卫吞掉后续调用
    if cmd not in _SERVICE_SUBCOMMANDS:
        setup_logging("cli")
    op_id = bind_op_id(component="cli", command=cmd)
    log.info(
        "cli_command_started",
        command=cmd, op_id=op_id,
    )
    # 把 op_id 挂到 ctx.obj 让业务代码可读（--json 输出时附上）
    ctx.obj = {"op_id": op_id}

# 退出码（04 §2.5.1）
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_CONFLICT = 3

# 冲突类错误码 → 退出码 3（其余失败 → 1）
_CONFLICT_CODES = {
    "TARGET_CONFLICT", "REMOVALS_REQUIRE_CONFIRM", "PARTIAL_REJECT",
}


def _not_implemented(name: str) -> None:
    """占位命令：初期阶段统一提示未实现，退出码 1（业务失败）。"""
    console.print(f"[yellow]`skillos {name}` 尚未实现（见 docs/03-开发排期.md）[/yellow]")
    raise typer.Exit(EXIT_FAIL)


def _make_engine(repo: str | None):
    """默认 ~/.skillos；沙盒部署经 SKILLOS_ROOT 指向容器内虚拟目录（T1）。"""
    from skillos.services.engine import EnvPaths, LocalEngine, default_env, resolve_home

    home = resolve_home()
    paths = default_env(home)
    if repo:
        repo_root = Path(repo)
        paths = EnvPaths(
            repo_root=repo_root,
            db_path=repo_root.parent / "skillos.db",
        )
    return LocalEngine(paths, home=home)


def _record_usage(engine, slug: str, op: str, ok: bool = True) -> None:
    """L1 采集（N-01）：CLI 是本地进程内入口，via=cli。失败只记日志，不影响命令结果。"""
    try:
        engine.usage.record(slug, op, via=VIA_CLI, ok=ok)
    except Exception:  # noqa: BLE001 —— 遥测不得让命令失败
        log.warning("usage_record_failed", op=op, skill_slug=slug)


def _all_skill_rows(engine):
    from skillos.store.index_repo import SkillQuery

    return engine.db.list_skills(SkillQuery(limit=10000))


def _all_slugs(engine) -> list[str]:
    return [r.slug for r in _all_skill_rows(engine)]


def _print_report(report) -> None:
    """分发结果逐目标呈现；冲突类退出码 3。

    三态而非两态（2026-10-17 修复）：只看 `wrote` 会把「dry-run 预览到要写」
    和「已是最新」都判成幂等跳过 —— 全新 skill 四端皆空时 dry-run 报"已是最新"。
    `action` 由 distribute 的前置判定给出，dry-run 与真跑同源（FR-1.3.7）。
    """
    for o in report.outcomes:
        if o.ok and o.action == "skip":
            console.print(f"  [green]✓[/green] {o.runtime_key}: 已是最新（幂等跳过）")
        elif o.ok and not o.wrote:
            extra = f"，未纳管资产保留 {len(o.skipped_assets)} 个" if o.skipped_assets else ""
            console.print(f"  [cyan]→[/cyan] {o.runtime_key}: 将部署（dry-run，未写入）{extra}")
        elif o.ok:
            extra = f"，未纳管资产保留 {len(o.skipped_assets)} 个" if o.skipped_assets else ""
            console.print(f"  [green]✓[/green] {o.runtime_key}: 已部署{extra}")
        elif o.error_code == "REMOVALS_REQUIRE_CONFIRM":
            console.print(f"  [yellow]⏸ {o.runtime_key}: 将删除以下内容，需确认[/yellow]")
            for p in o.held_back_removals:
                console.print(f"      - {p}")
        else:
            console.print(f"  [red]✗ {o.runtime_key}: {o.error_code} {o.detail or ''}[/red]")
    preview = any(o.ok and o.action == "write" and not o.wrote for o in report.outcomes)
    if report.ok:
        console.print("[green]预览完成（dry-run 未写入任何文件）[/green]" if preview
                      else "[green]分发完成[/green]")
        return
    codes = {o.error_code for o in report.outcomes}
    if codes & _CONFLICT_CODES:
        if any(o.error_code == "REMOVALS_REQUIRE_CONFIRM" for o in report.outcomes):
            console.print("[yellow]存在冲突待处理（--confirm-removals 可确认删除清单）[/yellow]")
        else:
            console.print("[yellow]存在冲突待处理[/yellow]")
        raise typer.Exit(EXIT_CONFLICT)
    raise typer.Exit(EXIT_FAIL)


@app.command("init")
def init_(
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径（默认 ~/.skillos/repo）"),
) -> None:
    """初始化本地环境：Git 仓库 + SQLite + repo 级 git 配置（05 P2-5）。"""
    engine = _make_engine(repo)
    for line in engine.init_env():
        console.print(f"[green]✓[/green] {line}")
    console.print(f"canonical 仓库: {engine.paths.repo_root}")


@app.command()
def scan(
    json_output: bool = typer.Option(False, "--json", help="以 JSON 输出清单"),
    roots: list[str] = typer.Option(None, "--roots", help="额外扫描根"),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """扫描四 Runtime 目录，输出 Skill 清单（FR-1.0.1 / FR-1.0.3）。"""
    engine = _make_engine(repo)
    found = engine.scan_all()
    if json_output:
        console.print_json(json.dumps(
            [f.model_dump() if hasattr(f, "model_dump") else vars(f) for f in found],
            ensure_ascii=False, default=str,
        ))
        return
    table = Table(title=f"发现 {len(found)} 个 Skill 目录")
    table.add_column("runtime")
    table.add_column("路径")
    table.add_column("skill")
    for f in found:
        rel = getattr(f, "rel_path", None) or getattr(f, "dir", "")
        table.add_row(f.runtime_key, str(rel), getattr(getattr(f, "meta", None), "name", "") or rel)
    console.print(table)


@app.command("import")
def import_(
    paths: list[str] = typer.Argument(None, help="待纳管目录（缺省=扫描四端全部）"),
    source: str = typer.Option("local", "--source", help="local | git:<url>（git 走 P2）"),
    json_output: bool = typer.Option(False, "--json"),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """纳管已有 Skill 目录（FR-1.0.16 / FR-1.1.3）。同名多副本见 04 §2.2.5 决策表。"""
    if source.startswith("git:"):
        console.print(
            "[yellow]`--source git:<url>` 尚未实现（P2）：当前只支持纳管本机已扫描到的目录[/yellow]"
        )
        raise typer.Exit(EXIT_USAGE)
    engine = _make_engine(repo)
    try:
        plan = engine.import_skills(paths=paths or None)
    except ValueError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(EXIT_FAIL) from None
    if json_output:
        console.print_json(json.dumps({
            "skills": [p.slug for p in plan.skills],
            "conflicts": [c.slug for c in plan.conflicts],
        }, ensure_ascii=False))
        return
    for p in plan.skills:
        copies = ", ".join(m.runtime_key for m in p.bindings)
        console.print(f"[green]✓[/green] {p.slug}（副本: {copies}）→ canonical")
    for c in plan.conflicts:
        console.print(f"[yellow]⏸ {c.slug}: 多副本内容不一致，需人工指定主副本[/yellow]")
    if plan.conflicts:
        raise typer.Exit(EXIT_CONFLICT)


@app.command()
def add(
    slug: str = typer.Argument(..., help="新 Skill 的 slug（kebab-case）"),
    name: str = typer.Option("", "--name", help="显示名（缺省=slug）"),
    description: str = typer.Option(
        ..., "--description", "-d",
        help="一句话描述（必填：fm.description.required 为 error 级门禁）",
    ),
    json_output: bool = typer.Option(False, "--json"),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """新建 Skill（FR-2.1.7）：canonical 模板 + commit + 索引，不自动 push。"""
    from skillos.services.engine import SkillValidationError

    engine = _make_engine(repo)
    try:
        result = engine.create_skill(
            slug, name=name, description=description,
            author="cli <cli@skillos.local>",
        )
    except SkillValidationError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(EXIT_FAIL) from None
    if json_output:
        console.print_json(json.dumps(result, ensure_ascii=False))
        return
    console.print(f"[green]✓[/green] 已创建 {slug}（commit {result['commit'][:10]}）")
    for f in result["gate"]:
        console.print(f"  [yellow]! {f['level']} {f['rule']}: {f['message']}[/yellow]")
    console.print(f"  下一步: skillos push --skill {slug}")


@app.command("rm")
def rm_(
    slug: str = typer.Argument(..., help="待删除 Skill 的 slug"),
    yes: bool = typer.Option(False, "--yes", "-y",
                             help="跳过交互确认（仍以 dry_run 列出的清单为准）"),
    json_output: bool = typer.Option(False, "--json"),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """删除 Skill（P3 CRUD）：只删 canonical + 索引；runtime 磁盘不动（红线 16）。

    流程（FR-1.0.26 ②）：先 dry_run 列出将消失的 canonical 路径，
    逐条展示并由用户确认后才执行。
    """
    from skillos.services.engine import SkillValidationError

    engine = _make_engine(repo)
    try:
        preview = engine.delete_skill(slug, dry_run=True)
    except SkillValidationError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(EXIT_FAIL) from None
    if not preview.get("ok"):
        console.print(f"[red]✗ {preview.get('error_code')}: {preview.get('detail')}[/red]")
        for p in preview.get("removals") or []:
            console.print(f"    - {p}")
        raise typer.Exit(EXIT_FAIL)
    removals = preview["removals"]
    console.print("[yellow]将删除以下 canonical 文件（runtime 磁盘不动）:[/yellow]")
    for p in removals:
        console.print(f"    - {p}")
    if not yes and not typer.confirm(f"确认删除 {slug}？"):
        console.print("已取消")
        return
    try:
        result = engine.delete_skill(slug, dry_run=False, confirmed_removals=removals)
    except SkillValidationError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(EXIT_FAIL) from None
    if not result.get("ok"):
        console.print(f"[red]✗ {result.get('error_code')}: {result.get('detail')}[/red]")
        raise typer.Exit(EXIT_FAIL)
    if json_output:
        console.print_json(json.dumps(result, ensure_ascii=False))
        return
    console.print(f"[green]✓[/green] 已删除 {slug}（commit {result['commit'][:10]}）")
    console.print("  runtime 目录已保留，可 re-import 或手动清理")


@app.command()
def status(
    json_output: bool = typer.Option(False, "--json"),
    skill: str = typer.Option(None, "--skill", help="按 slug 过滤"),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """四端漂移矩阵（FR-1.0.6）。"""
    engine = _make_engine(repo)
    cells = engine.status_matrix()
    slug_by_id = {r.id: r.slug for r in _all_skill_rows(engine)}
    rows = []
    for c in cells:
        slug = slug_by_id.get(c.skill_id, c.skill_id)
        if skill and slug != skill:
            continue
        label = str(c.state).split(".")[-1]
        if c.has_untracked_assets and label == "SYNCED":
            label = "SYNCED*"  # FR-1.0.20：含未纳管资产禁止显示纯 SYNCED
        rows.append({"skill": slug, "runtime": c.runtime_key, "state": label})
    if skill:   # 单 skill 查看算一次调用；全量矩阵是仪表盘，不计（N-01.1）
        _record_usage(engine, skill, "status")
    if json_output:
        console.print_json(json.dumps(rows, ensure_ascii=False))
        return
    table = Table(title="漂移矩阵")
    table.add_column("skill")
    table.add_column("runtime")
    table.add_column("state")
    _WARN_STATES = ("OUTDATED", "UNMANAGED", "MISSING", "ORPHANED")
    for r in rows:
        if r["state"] == "SYNCED":
            style = "green"
        elif "*" in r["state"] or r["state"] in _WARN_STATES:
            style = "yellow"
        else:
            style = "red"
        table.add_row(r["skill"], r["runtime"], f"[{style}]{r['state']}[/{style}]")
    console.print(table)


@app.command()
def push(
    slug: str = typer.Argument(None, help="目标 slug；与 --all 二选一"),
    all_: bool = typer.Option(False, "--all", help="全部 Skill"),
    to: str = typer.Option(None, "--to", help="目标 runtime 列表，逗号分隔"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    allow_gate_failure: bool = typer.Option(False, "--allow-gate-failure",
                                            help="门禁 error 时仍继续（仅 CLI，MCP 无此开关）"),
    confirm_removals: str = typer.Option(
        None, "--confirm-removals",
        help="确认删除的路径，逗号分隔（与 held_back 清单精确一致）",
    ),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """分发 canonical → Runtime（04 §2.2.2 十步协议）。冲突退出码 3。"""
    if bool(slug) == all_:
        console.print("[red]slug 与 --all 必须二选一[/red]")
        raise typer.Exit(EXIT_FAIL)
    engine = _make_engine(repo)
    runtime_keys = [k.strip() for k in to.split(",")] if to else None
    confirmed = (
        frozenset(p.strip() for p in confirm_removals.split(","))
        if confirm_removals
        else None
    )
    slugs = [slug] if slug else _all_slugs(engine)
    failed = False
    for s in slugs:
        console.print(f"[bold]push {s}[/bold]" + ("（dry-run）" if dry_run else ""))
        try:
            report = engine.push_skill(
                s, runtime_keys=runtime_keys, dry_run=dry_run,
                allow_gate_failure=allow_gate_failure, confirmed_removals=confirmed,
            )
        except ValueError as e:
            console.print(f"  [red]✗ {e}[/red]")
            raise typer.Exit(EXIT_FAIL) from e
        if report.outcomes and report.outcomes[0].error_code == "GATE_FAILED":
            console.print(f"  [red]✗ 门禁拦截（--allow-gate-failure 可继续）: "
                          f"{report.outcomes[0].detail}[/red]")
            raise typer.Exit(EXIT_FAIL)
        _record_usage(engine, s, "push", ok=report.ok)
        _print_report(report)
        if not report.ok:
            failed = True
    if failed:
        return  # _print_report 已按冲突/失败决定退出码
    raise typer.Exit(EXIT_OK)


@app.command()
def pull(
    slug: str = typer.Argument(...),
    from_: str = typer.Option(..., "--from", help="来源 runtime"),
    message: str = typer.Option(None, "--message", help="commit 说明（缺省自动生成）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只预览改动，不写 canonical"),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """回收 Runtime 改动为新版本（FR-1.1.6 / FR-2.2.6）。冲突退出码 3。

    与 MCP 的 `skillos_sync_pull` 同一实现（services.engine.pull_skill）：
    双向分歧（canonical 在基线之后也前进过）→ CONFLICT，交 Web 人工处理。
    """
    engine = _make_engine(repo)
    try:
        result = engine.pull_skill(slug, from_, dry_run=dry_run, message=message, by="cli")
    except ValueError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(EXIT_FAIL) from None
    _record_usage(engine, slug, "pull", ok=bool(result.get("ok")))
    if not result.get("ok"):
        console.print(f"[red]✗ {result.get('error_code')}: {result.get('detail')}[/red]")
        if result.get("error_code") == "CONFLICT":
            raise typer.Exit(EXIT_CONFLICT)
        raise typer.Exit(EXIT_FAIL)
    if result.get("noop"):
        console.print(f"[green]✓[/green] {slug}@{from_}: 无改动可回收（与基线一致）")
        raise typer.Exit(EXIT_OK)
    changed = result.get("changed") or []
    added = result.get("added") or []
    removed = result.get("removed") or []
    head = "预览：将回收" if dry_run else "已回收"
    console.print(f"[bold]{head} {slug} ← {from_}[/bold]")
    for label, items in (("修改", changed), ("新增", added), ("删除", removed)):
        if not items:
            continue
        console.print(f"  {label} {len(items)} 个:")
        for p in items:
            console.print(f"    - {p}")
    if dry_run:
        console.print("[yellow]dry-run：未写入任何文件（去掉 --dry-run 才落盘）[/yellow]")
        raise typer.Exit(EXIT_OK)
    console.print(f"[green]✓[/green] 新版本 {result['commit'][:10]}")
    console.print("[dim]注意：回收只动 canonical，其它 Runtime 需再 push[/dim]")


@app.command()
def versions(
    slug: str = typer.Argument(...),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """版本清单（具名命令，非 git 透传 —— OUT-14）。"""
    engine = _make_engine(repo)
    row = engine.db.get_skill_by_slug(slug)
    if row is None:
        console.print(f"[red]未纳管的 skill: {slug}[/red]")
        raise typer.Exit(EXIT_FAIL)
    _record_usage(engine, slug, "versions")
    table = Table(title=f"{slug} 版本历史")
    table.add_column("commit")
    table.add_column("时间")
    table.add_column("说明")
    for c in engine.git.log(path=f"skills/{slug}", limit=50):
        at = c.at[:16].replace("T", " ")  # ISO8601 字符串 → "YYYY-MM-DD HH:MM"
        table.add_row(c.sha[:10], at, c.message.splitlines()[0])
    console.print(table)


@app.command()
def diff(
    slug: str = typer.Argument(...),
    from_ref: str = typer.Option(None, "--from"),
    to_ref: str = typer.Option(None, "--to"),
    runtime: str = typer.Option(None, "--runtime"),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """版本间 diff（`--from/--to`）或端间 diff（`--runtime`）。二进制只显示「已变更」（T5）。"""
    engine = _make_engine(repo)
    if runtime:
        try:
            text = engine.diff_runtime(slug, runtime)
        except ValueError as e:
            console.print(f"[red]✗ {e}[/red]")
            raise typer.Exit(EXIT_FAIL) from None
        if not text.strip():
            console.print("[green]无差异（磁盘与该 Runtime 的 canonical 渲染结果一致）[/green]")
            return
        console.print(text, markup=False, highlight=False)
        return
    base = engine.git.resolve_ref(from_ref) if from_ref else engine.git.resolve_ref("HEAD")
    head = engine.git.resolve_ref(to_ref) if to_ref else engine.git.resolve_ref("HEAD")
    text = engine.git.diff(base, head, path=f"skills/{slug}")
    if not text.strip():
        console.print("[green]无差异[/green]")
        return
    console.print(text, markup=False, highlight=False)


@app.command()
def rollback(
    slug: str = typer.Argument(...),
    to_ref: str = typer.Option(..., "--to", help="目标 ref"),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """回滚到指定版本（checkout + 新 commit，不重写历史；先自动建安全快照）。"""
    engine = _make_engine(repo)
    row = engine.db.get_skill_by_slug(slug)
    if row is None:
        console.print(f"[red]未纳管的 skill: {slug}[/red]")
        raise typer.Exit(EXIT_FAIL)
    try:
        target = engine.git.resolve_ref(to_ref)
    except Exception as e:
        console.print(f"[red]无法解析 ref: {to_ref}[/red]")
        raise typer.Exit(EXIT_FAIL) from e
    new_sha = engine.git.rollback(target, [f"skills/{slug}"])
    console.print(
        f"[green]✓[/green] 已回滚 {slug} → {to_ref[:10]}，"
        f"安全快照与新 commit: {new_sha[:10]}"
    )
    console.print("[dim]注意：回滚只动 canonical，各 Runtime 需再 push[/dim]")


@app.command()
def check(
    skill: str = typer.Option(None, "--skill"),
    all_: bool = typer.Option(False, "--all", help="检查全部 Skill 并落库"),
    security_only: bool = typer.Option(
        False, "--security-only", help="只跑 category=security 的自定义规则（N-08.5，CI 用）",
    ),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """质量检查（FR-1.0.11 + N-03 自定义规则 + N-08 安全规则）。

    结果落 `skill_checks`（含本次 `ruleset_hash`，N-03.5）。`--security-only`
    只跑安全类规则，输出与退出码语义不变（有 error → 退出码 1）。
    """
    engine = _make_engine(repo)
    if not skill and not all_:
        console.print(
            "[yellow]用法: skillos check --skill <slug> | --all[/yellow]\n"
            "[dim]--security-only 只跑安全规则集（CI 用）[/dim]"
        )
        raise typer.Exit(EXIT_USAGE)
    slugs = engine.all_slugs() if all_ else [skill]
    if all_ and not slugs:
        console.print("[yellow]库里还没有 Skill（先 scan + import）[/yellow]")
        raise typer.Exit(EXIT_USAGE)

    has_error = False
    total = 0
    for slug in slugs:
        try:
            report = engine.checks.check(
                slug, security_only=security_only, include_untracked=True,
            )
        except Exception as e:  # noqa: BLE001 —— 单个 skill 读不出来不该中断 --all
            console.print(f"[red]{slug}: 无法读取 canonical: {e}[/red]")
            has_error = True
            continue
        findings = report.gate.findings
        total += len(findings)
        if len(slugs) > 1:
            console.print(f"\n[bold]{slug}[/bold]")
        for f in findings:
            style = "red" if f.level == "error" else ("yellow" if f.level == "warn" else "dim")
            console.print(f"[{style}]{f.level:5} {f.rule}: {f.message}[/{style}]")
        for row in report.bundle.invalid:
            console.print(f"[yellow]invalid {row['rule']}: {row['message']}[/yellow]")
        has_error = has_error or report.gate.has_error

    # E-04.2 / TQ-03：check 尾部顺带重算相似对（旁路能力，不影响退出码）
    try:
        engine.run_similarity()
    except Exception as e:  # noqa: BLE001 —— 相似提示坏了不该让检查结论变得不可用
        console.print(f"[yellow]相似比对失败：{e}[/yellow]")

    ruleset = engine.rules.ruleset_hash()
    console.print(
        f"\n[dim]规则集 {ruleset[:10] or '（无自定义规则）'} · "
        f"{len(slugs)} 个 Skill · {total} 条 finding[/dim]"
    )
    if not has_error and total == 0:
        console.print("[green]✓ 检查通过，无发现[/green]")
        raise typer.Exit(EXIT_OK)
    if has_error:
        raise typer.Exit(EXIT_FAIL)


@app.command()
def doctor() -> None:
    """环境自检（FR-1.1.8）：11 项检查逐项返回 ok/warn/fail，见 04 §2.5.1 表。"""
    from skillos.cli.doctor import run_doctor

    failures = run_doctor(console)
    raise typer.Exit(EXIT_FAIL if failures else EXIT_OK)


@app.command()
def devices(
    json_output: bool = typer.Option(False, "--json"),
    revoke: str = typer.Argument(None, help="撤销指定设备 id"),
) -> None:
    """设备清单 / 撤销（FR-1.2.8，P3 未实现：当前请用 Web 的 /api/devices 人工路径）。"""
    _not_implemented("devices（P3：设备撤销走 Web 人工路径）")


@app.command()
def daemon(
    cloud: str = typer.Option("ws://127.0.0.1:9080/ws/agent", "--cloud",
                             help="云端 WS URL"),
    pair: str = typer.Option(None, "--pair", help="6 位配对码"),
) -> None:
    """启动本地 daemon（WS 出站连接云端，04 §2.5.2）。

    沙盒部署（FR-2.4.3）：容器内经 SKILLOS_ROOT 把 home 指向虚拟目录，
    隔离靠文件系统边界，本命令与本地部署完全同一条代码路径。
    """
    from skillos.daemon.main import main as daemon_main

    daemon_main(cloud_url=cloud, pair_code=pair)


@app.command()
def mcp() -> None:
    """以 stdio 启动 MCP Server（FR-1.3.x，八工具全 dry_run、无 force）。"""
    from skillos.mcp.server import main as mcp_main

    mcp_main()


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址（容器内用 0.0.0.0）"),
    port: int = typer.Option(9080, "--port", "-p", help="监听端口"),
    reload: bool = typer.Option(False, "--reload", help="改动 Python 源码后自动重启（仅开发）"),
    dev: bool = typer.Option(False, "--dev", help="前端走 vite 热更：不挂载 SPA"),
    open_browser: bool = typer.Option(False, "--open", help="启动后自动打开浏览器"),
    db_url: str = typer.Option(
        None, "--db-url", help="覆盖 SKILLOS_DB_URL（默认 ./skillos-cloud.db）"
    ),
) -> None:
    """启动 Web 控制台（FastAPI 与 SPA 同进程，04 §1.4 容器 A）。

    单端口形态下「起前端」= 起后端：`mount_web()` 自动定位 SPA 产物
    （随包 `skillos/web_dist` / 源码 `web/dist` / 发行包 `runtime/web`）。

    `--dev` 不挂载 SPA，前端另起 `npm run dev`（vite 5173，proxy /api 与 /ws
    到本进程）—— 由 `SKILLOS_WEB_DIST=off` 表达，前端源码改动即时生效。
    """
    import webbrowser
    from threading import Timer

    import uvicorn

    from skillos.api.web import resolve_web_dist

    if db_url:
        os.environ["SKILLOS_DB_URL"] = db_url
    if dev:
        os.environ["SKILLOS_WEB_DIST"] = "off"
        dist, note = None, "dev 模式：前端由 vite 提供"
    else:
        dist, note = resolve_web_dist()

    shown_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{shown_host}:{port}/"
    console.print(f"[bold]Skill OS 控制台[/bold] → {url}")
    if dist is not None:
        console.print(f"  SPA 产物：{dist}")
    else:
        # 装好包却打不开是最容易劝退的一种失败，这里把三条出路一次说清
        console.print(f"  [yellow]SPA 未挂载（{note}）[/yellow]")
        console.print("    源码仓库：cd web && npm run build")
        console.print("    wheel 安装：确认包内有 skillos/web_dist（见 setup.py 的 stage 钩子）")
        console.print("    任意来源：SKILLOS_WEB_DIST=<dist 目录> skillos serve")
    if dev:
        console.print("  前端热更：cd web && npm run dev → http://127.0.0.1:5173/")

    if open_browser:
        # 延迟开：uvicorn 还没绑端口时打开会先拿到连接拒绝
        Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run("skillos.api.asgi:app", host=host, port=port, reload=reload)


@app.command()
def logs() -> None:
    """查看 / 过滤本地日志（FR-1.1.12）。

    与 `skillos log <slug>`（git log 不可变账本）职责分离：
    本命令 = 运行流水；`log` 子命令 = 资产身份历史。
    """
    from skillos.cli.logs_cmd import logs_cmd

    logs_cmd()


# ---------------------------------------------------------------- 体检报告（N-02）

@app.command()
def report(
    last: bool = typer.Option(False, "--last", help="只看最近一份，不生成新报告"),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """体检报告（N-02）：默认生成一份新报告（自上一份以来的增量）。

    无 daemon 时的降级路径（N-02.1）；daemon 在线时按 `report.cadence` 自动生成。
    """
    engine = _make_engine(repo)
    if last:
        items = engine.reports.list_reports(limit=1)
        if not items:
            console.print("[yellow]还没有报告，先跑一次 `skillos report` 生成基线[/yellow]")
            raise typer.Exit(EXIT_USAGE)
        data = engine.reports.get_report(items[0]["id"])
        if data is None:   # 列表读到了、详情读不到 = DB 异常，不静默当"没有报告"
            console.print("[red]✗ 报告详情读取失败[/red]")
            raise typer.Exit(EXIT_FAIL)
    else:
        data = engine.reports.generate()

    period = data.get("period") or {}
    console.print(
        f"[bold]体检报告[/bold] {period.get('start', '?')} → {period.get('end', '?')}"
    )
    if data.get("unchanged"):
        console.print("[green]无变化[/green]（N-02.6：无变化也留痕，周期连续可查）")

    drift = data.get("drift") or {}
    console.print("\n[bold]① 漂移增量[/bold]（MODIFIED / CONFLICT / MISSING）")
    for state, counts in (drift.get("counts") or {}).items():
        console.print(
            f"  {state:<9} {counts.get('now', 0):>3} 项"
            f"（较上期 {counts.get('delta', 0):+d}）"
        )
    for item in drift.get("new") or []:
        console.print(
            f"    [yellow]+ {item.get('state')} {item.get('slug')}"
            f" @ {item.get('runtime_key')}[/yellow]"
        )

    gate = data.get("gate") or {}
    console.print(f"\n[bold]② 门禁 error 新增[/bold] {gate.get('new_count', 0)} 项")
    for item in gate.get("new") or []:
        console.print(f"    [red]+ {item.get('slug')} · {item.get('rule')}[/red]")

    decay = data.get("decay") or {}
    console.print(f"\n[bold]③ 折旧 ok→degraded[/bold] {decay.get('count', 0)} 项")
    for slug in decay.get("newly_degraded") or []:
        console.print(f"    [yellow]+ {slug}[/yellow]")

    health = data.get("health") or {}
    console.print(f"\n[bold]④ 健康度降幅 Top5[/bold]（均值 {health.get('avg')}）")
    for item in health.get("drops") or []:
        console.print(
            f"    {item.get('slug')}: {item.get('from')} → {item.get('to')}"
            f"（{item.get('delta'):+d}）"
        )
    raise typer.Exit(EXIT_OK)


# ---------------------------------------------------------------- 遥测（N-11.4 / N-11.9）

telemetry_app = typer.Typer(name="telemetry", help="Runtime 会话遥测（L2，token 为估算口径）")
path_app = typer.Typer(name="path", help="Runtime 会话路径：手动指定优先于自动探测")
telemetry_app.add_typer(path_app, name="path")
app.add_typer(telemetry_app, name="telemetry")


def _telemetry_service(engine):
    from skillos.services.telemetry_service import TelemetryService

    # 部署根来自 runtimes 表：skill 关联只认「命中部署目录」的会话行（N-11.4）
    deploy_roots = [Path(r.root_path) for r in engine.db.list_runtimes()]
    return TelemetryService(
        store=engine.db, settings=engine.settings,
        home=engine.home, deploy_roots=deploy_roots,
    )


@telemetry_app.command("import")
def telemetry_import(
    runtime: str = typer.Argument("claude-code", help="Runtime 适配器名（见 adapters/telemetry）"),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """导入 Runtime 本地会话日志。token 归属为**估算口径**（N-11.3），不可用于计费。"""
    from skillos.services.telemetry_service import TelemetryPathError

    engine = _make_engine(repo)
    try:
        result = _telemetry_service(engine).import_runtime(runtime)
    except TelemetryPathError as exc:
        # N-11.9④：不静默产空结果 —— 报错并列出已尝试路径
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(EXIT_FAIL) from exc

    # 立刻重算日聚合，让 token 马上可见（聚合幂等，重复跑无害）
    engine.usage.aggregate()

    table = Table(title=f"{runtime} 会话导入（token 为估算口径）")
    table.add_column("项目")
    table.add_column("值", justify="right")
    table.add_row("会话文件", str(result.files))
    table.add_row("总行数", str(result.total_lines))
    table.add_row("usage 行", str(result.usage_lines))
    table.add_row("跳过行（损坏/无时间戳）", str(result.skipped_lines))
    table.add_row("归属 skill 数", str(len(result.by_skill)))
    table.add_row("未归属 token", str(sum(result.unattributed.values())))
    table.add_row("写入事件", str(result.events_written))
    console.print(table)
    for skill_id, totals in sorted(result.by_skill.items()):
        console.print(
            f"  {skill_id}: in={totals['tokens_in']} out={totals['tokens_out']}"
            f" cache={totals['cache_read']}"
        )
    raise typer.Exit(EXIT_OK)


@path_app.command("set")
def telemetry_path_set(
    runtime: str = typer.Argument(..., help="Runtime 适配器名"),
    paths: list[str] = typer.Argument(..., help="目录列表（多实例/多 profile 可给多个）"),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """手动指定会话目录（覆盖自动探测，N-11.9②③）。"""
    engine = _make_engine(repo)
    for raw in paths:
        if not Path(raw).expanduser().is_dir():
            # N-11.9④：路径不合法当场报错，不留到 import 时才炸
            console.print(f"[red]✗ 不是目录: {raw}[/red]")
            raise typer.Exit(EXIT_FAIL)
    engine.settings.set_runtime_paths(runtime, [str(Path(p).expanduser()) for p in paths])
    console.print(f"[green]✓[/green] {runtime} 手动路径 {len(paths)} 个（自动探测已跳过）")
    raise typer.Exit(EXIT_OK)


@path_app.command("show")
def telemetry_path_show(
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """查看手动路径配置（未配置则显示为空，不代表没有自动探测）。"""
    engine = _make_engine(repo)
    configured = engine.settings.runtime_paths()
    if not configured:
        console.print("[dim]未配置手动路径（import 时走适配器自动探测）[/dim]")
        raise typer.Exit(EXIT_OK)
    table = Table(title="Runtime 会话路径（手动）")
    table.add_column("runtime")
    table.add_column("路径")
    for key, items in sorted(configured.items()):
        table.add_row(key, "\n".join(items))
    console.print(table)
    raise typer.Exit(EXIT_OK)


@path_app.command("clear")
def telemetry_path_clear(
    runtime: str = typer.Argument(..., help="Runtime 适配器名"),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """清除手动路径，回到自动探测。"""
    engine = _make_engine(repo)
    engine.settings.set_runtime_paths(runtime, [])
    console.print(f"[green]✓[/green] 已清除 {runtime} 的手动路径")
    raise typer.Exit(EXIT_OK)


if __name__ == "__main__":
    app()
