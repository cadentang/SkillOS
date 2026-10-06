"""CLI 入口层（FR-1.1.x，04 §2.5.1 / §4.4）。

- 命令集即白名单（OUT-14）：versions / diff / rollback 是三个具名命令，
  不是 skillos git <args>；daemon WS 任务表与之一一对应。
- 退出码：0 成功 / 1 业务失败 / 2 参数错误 / 3 冲突待处理。
- CLI 与 MCP 完全不走云端（FR-2.4.3 / NFR-1），离线可用。
"""

from __future__ import annotations

import sys

import typer
from rich.console import Console

# 05 P0-2：中文 Windows 控制台默认 cp936，输出中文 skill 名必抛 UnicodeEncodeError。
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


def _not_implemented(name: str) -> None:
    """占位命令：初始化阶段统一提示未实现，退出码 1（业务失败）。"""
    console.print(f"[yellow]`skillos {name}` 尚未实现（见 docs/03-开发排期.md）[/yellow]")
    raise typer.Exit(EXIT_FAIL)


@app.command("init")
def init_(
    repo: str = typer.Option(
        None, "--repo", help="canonical 仓库路径（默认 ~/.skillos/repo）"
    ),
) -> None:
    """初始化本地环境：Git 仓库 + SQLite + repo 级 git 配置（05 P2-5）。"""
    _not_implemented("init")


@app.command()
def scan(
    json_output: bool = typer.Option(False, "--json", help="以 JSON 输出清单"),
    roots: list[str] = typer.Option(None, "--roots", help="额外扫描根"),
) -> None:
    """扫描四个 Runtime 目录，输出 Skill 清单（FR-1.0.1 / FR-1.0.3）。"""
    _not_implemented("scan")


@app.command("import")
def import_(
    paths: list[str] = typer.Argument(..., help="待纳管目录"),
    source: str = typer.Option("local", "--source", help="local | git:<url>"),
) -> None:
    """纳管已有 Skill 目录（FR-1.0.16 / FR-1.1.3）。同 slug 多副本见 04 §2.2.5 决策表。"""
    _not_implemented("import")


@app.command()
def status(
    json_output: bool = typer.Option(False, "--json"),
    skill: str = typer.Option(None, "--skill", help="按 slug 过滤"),
) -> None:
    """四端漂移矩阵（FR-1.0.6）。"""
    _not_implemented("status")


@app.command()
def push(
    slug: str = typer.Argument(None, help="目标 slug；与 --all 二选一"),
    all_: bool = typer.Option(False, "--all", help="全部 Skill"),
    to: str = typer.Option(None, "--to", help="目标 runtime 列表，逗号分隔"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """分发 canonical → Runtime（04 §2.2.2 十步协议）。冲突退出码 3。"""
    _not_implemented("push")


@app.command()
def pull(
    slug: str = typer.Argument(...),
    from_: str = typer.Option(..., "--from", help="来源 runtime"),
    message: str = typer.Option(None, "--message"),
) -> None:
    """回收 Runtime 改动为新版本（FR-2.2.6）。"""
    _not_implemented("pull")


@app.command()
def versions(slug: str = typer.Argument(...)) -> None:
    """版本清单（具名命令，非 git 透传 —— OUT-14）。"""
    _not_implemented("versions")


@app.command()
def diff(
    slug: str = typer.Argument(...),
    from_ref: str = typer.Option(None, "--from"),
    to_ref: str = typer.Option(None, "--to"),
    runtime: str = typer.Option(None, "--runtime"),
) -> None:
    """版本间 / 端间 diff。二进制第一版只显示「已变更」（T5）。"""
    _not_implemented("diff")


@app.command()
def rollback(
    slug: str = typer.Argument(...),
    to_ref: str = typer.Option(..., "--to", help="目标 ref"),
) -> None:
    """回滚到指定版本（checkout + 新 commit，不重写历史）。"""
    _not_implemented("rollback")


@app.command()
def check(skill: str = typer.Option(None, "--skill")) -> None:
    """质量检查（FR-1.0.11，规则表见 core/algorithm/checks.py）。"""
    _not_implemented("check")


@app.command()
def doctor() -> None:
    """环境自检（FR-1.1.8）：9 项检查逐项返回 ok/warn/fail，见 04 §2.5.1 表。"""
    _not_implemented("doctor")


@app.command()
def devices(
    json_output: bool = typer.Option(False, "--json"),
    revoke: str = typer.Argument(None, help="撤销指定设备 id"),
) -> None:
    """设备清单 / 撤销（FR-1.2.8）。"""
    _not_implemented("devices")


@app.command()
def daemon(
    cloud: str = typer.Option(None, "--cloud", help="云端 URL"),
    pair: str = typer.Option(None, "--pair", help="6 位配对码"),
) -> None:
    """启动本地 daemon（WS 出站连接云端，04 §2.5.2）。"""
    _not_implemented("daemon")


@app.command()
def mcp() -> None:
    """以 stdio 启动 MCP Server（FR-1.3.x）。"""
    _not_implemented("mcp")


if __name__ == "__main__":
    app()
