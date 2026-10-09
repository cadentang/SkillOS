"""`skillos logs` 子命令（FR-1.1.12）。

读取 `~/.skillos/logs/<mode>.log`，按 level / op-id / since 过滤后输出。
与 `skillos log <slug>`（git log 不可变账本，FR-1.1.10）**职责分离**：
- `skillos log <slug>` = 资产身份历史（git commit）
- `skillos logs` = 运行流水（structlog event）

**不在范围**（避免过度设计）：
- 远程日志聚合（个人 / 18 天约束）
- OpenTelemetry trace_id 接入
- 实时 tail 模式（`--follow` 仅作占位，本期不支持，因为日志写者（daemon）常驻）
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from skillos.services.engine import resolve_home

# 可选 mode（与 core/logging.py 的 LogMode 对齐）
_LOG_MODES: tuple[str, ...] = ("daemon", "api", "mcp")
_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# JSON 时间戳前缀解析（structlog ISO 含毫秒）
_TS_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2})?)"
)


@dataclass
class _LogEvent:
    ts: datetime | None
    level: str
    logger: str
    msg: str
    raw: str


def _iter_log_lines(
    path: Path, tail: int = 0, follow: bool = False
) -> Iterator[str]:
    """读日志文件。支持 tail 模式与 follow（follow 占位，本期不真做）。"""
    if not path.exists():
        return
    if tail > 0:
        # tail：按块读取，找到 tail 行起点
        with path.open("r", encoding="utf-8", errors="replace") as f:
            # 简单实现：读全部再 slice（10MB 文件足够快；复杂实现留给 P5）
            lines = f.readlines()
            yield from lines[-tail:]
        return
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            yield line
    if follow:
        # 占位：daemon / api 通常常驻，--follow 需配套 follow 子进程退出信号；
        # 本期不实装，提示用户用 `tail -f` 替代
        sys.stderr.write(
            "[yellow]--follow 暂未实装（建议直接用 `tail -F ~/.skillos/logs/daemon.log`）[/yellow]\n"
        )


def _parse_line(line: str) -> _LogEvent | None:
    """解析一行 JSON / key=value 为 _LogEvent。"""
    line = line.strip()
    if not line:
        return None
    # JSON
    if line.startswith("{"):
        try:
            rec = json.loads(line)
        except ValueError:
            return None
        ts_str = str(rec.get("ts") or rec.get("time") or "")
        ts = _parse_ts(ts_str)
        level = str(rec.get("level") or "").upper()
        logger = str(rec.get("logger") or rec.get("name") or "")
        msg = str(rec.get("event") or "") + " " + " ".join(
            f"{k}={v}" for k, v in rec.items()
            if k not in ("ts", "level", "logger", "event", "pid", "tid", "op_id")
        )
        return _LogEvent(ts=ts, level=level, logger=logger, msg=msg.strip(), raw=line)
    # key=value 形态（CLI human 模式）
    parts = line.split()
    kv: dict[str, str] = {}
    for p in parts:
        if "=" in p:
            k, _, v = p.partition("=")
            kv[k] = v
    return _LogEvent(
        ts=_parse_ts(kv.get("ts", "")),
        level=kv.get("level", "").upper(),
        logger=kv.get("logger", ""),
        msg=" ".join(p for p in parts if "=" not in p),
        raw=line,
    )


def _parse_ts(s: str) -> datetime | None:
    """解析 ISO8601；解析失败返回 None。"""
    if not s:
        return None
    m = _TS_PATTERN.match(s)
    if m:
        ts_str = m.group(1).replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(ts_str)
        except ValueError:
            return None
    return None


def _level_ok(level: str, min_level: str) -> bool:
    """判断 level 是否 ≥ min_level。"""
    try:
        return _LEVELS.index(level.upper()) >= _LEVELS.index(min_level.upper())
    except ValueError:
        return True  # 解析不出来不过滤


def _print_event(console: Console, ev: _LogEvent, *, json_output: bool) -> None:
    if json_output:
        out = {
            "ts": ev.ts.isoformat() if ev.ts else None,
            "level": ev.level,
            "logger": ev.logger,
            "msg": ev.msg,
        }
        console.print_json(json.dumps(out, ensure_ascii=False))
        return
    # human：ts level logger msg（着色按 level）
    ts_str = ev.ts.strftime("%H:%M:%S") if ev.ts else "??:??:??"
    style = {
        "ERROR": "red", "CRITICAL": "red bold",
        "WARNING": "yellow",
        "INFO": "green", "DEBUG": "dim",
    }.get(ev.level, "")
    line = f"[dim]{ts_str}[/dim] {ev.level:<8} {ev.logger}"
    if style:
        line = f"[dim]{ts_str}[/dim] [{style}]{ev.level:<8}[/{style}] {ev.logger}"
    console.print(f"{line}  {ev.msg}")


def logs_cmd(
    mode: str = typer.Option("daemon", "--mode", help="daemon / api / mcp"),
    tail: int = typer.Option(100, "--tail", help="末尾 N 行（0 = 全部）"),
    level: str = typer.Option("DEBUG", "--level",
                              help="最低级别（DEBUG/INFO/WARNING/ERROR）"),
    since: str = typer.Option(None, "--since", help="起始时间（YYYY-MM-DD 或 ISO8601）"),
    op_id: str = typer.Option(None, "--op-id", help="按 op_id 过滤"),
    follow: bool = typer.Option(False, "--follow", help="（占位）实时跟随；本期不实装"),
    json_output: bool = typer.Option(False, "--json", help="JSON 行输出"),
) -> None:
    """查看本地日志（FR-1.1.12）。"""
    if mode not in _LOG_MODES:
        typer.echo(f"[red]未知 mode: {mode}（可选 {_LOG_MODES}）[/red]", err=True)
        raise typer.Exit(1)
    if level.upper() not in _LEVELS:
        typer.echo(f"[red]未知 level: {level}（可选 {_LEVELS}）[/red]", err=True)
        raise typer.Exit(1)

    console = Console()
    home = resolve_home()
    log_path = home / ".skillos" / "logs" / f"{mode}.log"

    if not log_path.exists():
        console.print(
            f"[yellow]{log_path} 不存在（mode={mode} 的进程尚未启动过）[/yellow]"
        )
        return

    # since 解析
    since_ts: datetime | None = None
    if since:
        # 接受 YYYY-MM-DD 或完整 ISO8601
        try:
            since_ts = datetime.fromisoformat(since)
        except ValueError:
            try:
                since_ts = datetime.strptime(since, "%Y-%m-%d")
            except ValueError:
                typer.echo(f"[red]无法解析 --since: {since}[/red]", err=True)
                raise typer.Exit(1) from None

    op_filter = op_id
    n_total = 0
    n_printed = 0
    for line in _iter_log_lines(log_path, tail=tail, follow=follow):
        n_total += 1
        ev = _parse_line(line)
        if ev is None:
            continue
        if not _level_ok(ev.level, level):
            continue
        if since_ts and ev.ts and ev.ts < since_ts:
            continue
        if op_filter and op_filter not in line:
            # 在 raw line 里查 op_id 是因为 JSON 字段可能不同名
            continue
        _print_event(console, ev, json_output=json_output)
        n_printed += 1
    if not json_output:
        console.print(
            f"[dim]{log_path}：扫描 {n_total} 行，过滤后 {n_printed} 行[/dim]"
        )