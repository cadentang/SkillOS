"""CLI 入口层（FR-1.1.x，04 §2.5.1 / §4.4）。

- 命令集即白名单（OUT-14）：versions / diff / rollback 是三个具名命令，
  不是 skillos git <args>；daemon WS 任务表与之一一对应。
- 退出码：0 成功 / 1 业务失败 / 2 参数错误 / 3 冲突待处理。
- CLI 与 MCP 完全不走云端（FR-2.4.3 / NFR-1），离线可用。
- 分层红线：本层只做参数解析与呈现，业务全部走 services.engine。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

# 05 P0-2：中文 + Windows 控制台默认 cp936，输出中文 skill 名必抛 UnicodeEncodeError。
# 入口处强制 UTF-8，必须先于任何输出执行。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(
    name="skillos",
    help="Skill OS —— 个人 AI Skill 操作系统（canonical Git 仓库 + 四 Runtime 分发）",
    no_args_is_help=True,
)
console = Console()

# 退出码（04 §2.5.1）
EXIT_OK = 0
EXIT_FAIL = 1
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


def _all_skill_rows(engine):
    from skillos.store.index_repo import SkillQuery

    return engine.db.list_skills(SkillQuery(limit=10000))


def _all_slugs(engine) -> list[str]:
    return [r.slug for r in _all_skill_rows(engine)]


def _print_report(report) -> None:
    """分发结果逐目标呈现；冲突类退出码 3。"""
    for o in report.outcomes:
        if o.ok and not o.wrote:
            console.print(f"  [green]✓[/green] {o.runtime_key}: 已是最新（幂等跳过）")
        elif o.ok:
            extra = f"，未纳管资产保留 {len(o.skipped_assets)} 个" if o.skipped_assets else ""
            console.print(f"  [green]✓[/green] {o.runtime_key}: 已部署{extra}")
        elif o.error_code == "REMOVALS_REQUIRE_CONFIRM":
            console.print(f"  [yellow]⏸ {o.runtime_key}: 将删除以下内容，需确认[/yellow]")
            for p in o.held_back_removals:
                console.print(f"      - {p}")
        else:
            console.print(f"  [red]✗ {o.runtime_key}: {o.error_code} {o.detail or ''}[/red]")
    if report.ok:
        console.print("[green]分发完成[/green]")
        return
    codes = {o.error_code for o in report.outcomes}
    if codes & _CONFLICT_CODES:
        console.print("[yellow]存在冲突待处理（--confirm-removals 可确认删除清单）[/yellow]")
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
    source: str = typer.Option("local", "--source", help="local | git:<url>"),
    json_output: bool = typer.Option(False, "--json"),
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """纳管已有 Skill 目录（FR-1.0.16 / FR-1.1.3）。同名多副本见 04 §2.2.5 决策表。"""
    if paths:
        _not_implemented("import <paths>（指定路径纳管走 P2）")
    engine = _make_engine(repo)
    plan = engine.import_skills()
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
    message: str = typer.Option(None, "--message"),
) -> None:
    """回收 Runtime 改动为新版本（FR-2.2.6）。"""
    _not_implemented("pull")


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
    """版本间 diff。二进制第一版只显示「已变更」（T5）。"""
    if runtime:
        _not_implemented("diff --runtime（端间 diff 走 P3）")
    engine = _make_engine(repo)
    base = engine.git.resolve_ref(from_ref) if from_ref else engine.git.resolve_ref("HEAD")
    head = engine.git.resolve_ref(to_ref) if to_ref else engine.git.resolve_ref("HEAD")
    text = engine.git.diff(base, head, path=f"skills/{slug}")
    if not text.strip():
        console.print("[green]无差异[/green]")
        return
    console.print(text)


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
    repo: str = typer.Option(None, "--repo", help="canonical 仓库路径"),
) -> None:
    """质量检查（FR-1.0.11，规则表见 core/algorithm/checks.py）。"""
    engine = _make_engine(repo)
    if not skill:
        _not_implemented("check --all（全量检查走 P2）")
    try:
        result = engine.gate_of(skill)
    except Exception as e:
        console.print(f"[red]无法读取 canonical: {e}[/red]")
        raise typer.Exit(EXIT_FAIL) from e
    for f in result.findings:
        style = "red" if f.level == "error" else ("yellow" if f.level == "warn" else "dim")
        console.print(f"[{style}]{f.level:5} {f.rule}: {f.message}[/{style}]")
    if not result.findings:
        console.print("[green]✓ 检查通过，无发现[/green]")
        raise typer.Exit(EXIT_OK)
    if result.has_error:
        raise typer.Exit(EXIT_FAIL)


@app.command()
def doctor() -> None:
    """环境自检（FR-1.1.8）：10 项检查逐项返回 ok/warn/fail，见 04 §2.5.1 表。"""
    from skillos.cli.doctor import run_doctor

    failures = run_doctor(console)
    raise typer.Exit(EXIT_FAIL if failures else EXIT_OK)


@app.command()
def devices(
    json_output: bool = typer.Option(False, "--json"),
    revoke: str = typer.Argument(None, help="撤销指定设备 id"),
) -> None:
    """设备清单 / 撤销（FR-1.2.8）。"""
    _not_implemented("devices")


@app.command()
def daemon(
    cloud: str = typer.Option("ws://127.0.0.1:8000/ws/agent", "--cloud",
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


if __name__ == "__main__":
    app()
