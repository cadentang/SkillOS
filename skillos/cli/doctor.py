"""`skillos doctor` 环境自检（FR-1.1.8，04 §2.5.1 九项检查表）。

九项逐项 ok / warn / fail。fail 数返回给调用方决定退出码；warn 不阻塞。
第 6 项（WSL 边界）是演示防砸关键：daemon 跑 WSL 而 Runtime 在 Windows 时
同步会「成功」但 Agent 看不到效果（05 P0-6 / NFR-7.7）。

第 11 项（T23 / 04 §6.2.7）：日志目录可写 + 单文件大小 + 最近 ERROR 数。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from skillos.services.engine import LocalEngine, default_env, resolve_home

_STYLE = {"ok": "green", "warn": "yellow", "fail": "red"}
_ICON = {"ok": "✓", "warn": "!", "fail": "✗"}

# 云同步目录特征（05 P0-5）：repo 放里面会被双向同步破坏 .git
_CLOUD_DIR_MARKERS = ("OneDrive", "iCloud", "Dropbox", "Google Drive", "百度网盘", "坚果云")


@dataclass(frozen=True)
class CheckResult:
    item: str
    level: str  # ok / warn / fail
    detail: str


def _check_repo_location(repo_root: Path) -> CheckResult:
    parts = [p for p in repo_root.resolve().parts]
    hit = next((m for m in _CLOUD_DIR_MARKERS if any(m in p for p in parts)), None)
    if hit:
        return CheckResult(
            "repo 位置", "fail",
            f"仓库位于云同步目录（{hit}）内，双向同步会破坏 .git（05 P0-5），请迁移",
        )
    return CheckResult("repo 位置", "ok", str(repo_root))


def _check_console_encoding() -> CheckResult:
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    if "utf" not in enc:
        return CheckResult("控制台编码", "fail", f"stdout 编码为 {enc}，中文输出必崩（05 P0-2）")
    return CheckResult("控制台编码", "ok", f"stdout={enc}")


def _check_git(engine: LocalEngine) -> CheckResult:
    try:
        ver = subprocess.run(
            ["git", "--version"], capture_output=True, timeout=10,
        ).stdout.decode(errors="replace").strip()
    except (OSError, subprocess.TimeoutExpired):
        return CheckResult("git 可用性", "fail", "git 不在 PATH 中")
    if not (engine.paths.repo_root / ".git").exists():
        return CheckResult("git 可用性", "warn", "仓库未初始化（先 skillos init）")
    cfg = subprocess.run(
        ["git", "-C", str(engine.paths.repo_root), "config", "core.autocrlf"],
        capture_output=True, timeout=10,
    ).stdout.decode().strip()
    if cfg != "false":
        return CheckResult(
            "git 可用性", "fail",
            f"repo 级 core.autocrlf={cfg or '未设置'}，checkout 会改字节导致漂移误报（05 P0-1）",
        )
    return CheckResult("git 可用性", "ok", f"{ver}，autocrlf=false 已生效")


def _check_lock(engine: LocalEngine) -> CheckResult:
    lock = engine.paths.lock_dir / ".lock"
    if not lock.exists():
        return CheckResult("锁文件", "ok", "无锁文件")
    # filelock 释放后锁文件仍在（内容是持有者 pid）；只有 pid 已死才算陈旧
    try:
        pid = int(lock.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        pid = 0
    if pid:
        try:
            os.kill(pid, 0)  # 进程存活探测（Windows 上也走此路径）
        except OSError:
            return CheckResult("锁文件", "warn", f"陈旧锁（pid {pid} 已退出），可安全删除")
        return CheckResult("锁文件", "warn", f"锁正被 pid {pid} 持有")
    return CheckResult("锁文件", "ok", "空闲锁文件（无持有者）")


def _check_residue(engine: LocalEngine) -> CheckResult:
    """分发残留（FR-1.0.15）：四端部署根下的 *.old / .tmp-* 目录。

    按目录名形态直接扫描（含未纳管 skill 的残留）；恢复动作由 push 前的
    定向检查执行，doctor 只报告。
    """
    found: list[str] = []
    for rt in engine.db.list_runtimes():
        root = Path(rt.root_path)
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.name.endswith(".old") or ".tmp-" in child.name:
                found.append(f"{rt.key}/{child.name}")
    if found:
        return CheckResult(
            "分发残留", "fail",
            f"发现 {len(found)} 处残留（{', '.join(found[:3])}），"
            "push 前会自动定向恢复（FR-1.0.15）",
        )
    return CheckResult("分发残留", "ok", "无 .old / .tmp-* 残留")


def _check_wsl_boundary(engine: LocalEngine) -> CheckResult:
    """WSL 边界（05 P0-6）：本进程与 Runtime 根必须同一文件系统命名空间。"""
    in_wsl = "WSL_DISTRO_NAME" in os.environ
    root = engine.paths.repo_root.resolve()
    unc = str(root).startswith(r"\\wsl") or str(root).startswith("//wsl")
    if in_wsl and root.drive.startswith("\\\\"):
        return CheckResult("WSL 边界", "fail", "WSL 内跨 Windows UNC 路径访问（05 P0-6）")
    if unc:
        return CheckResult("WSL 边界", "fail", "Runtime 根在 WSL UNC 路径上，Windows 侧不可靠")
    return CheckResult("WSL 边界", "ok", "同源命名空间" if not in_wsl else "WSL 内本地路径")


def _check_runtime_dirs(engine: LocalEngine) -> CheckResult:
    bad: list[str] = []
    for rt in engine.db.list_runtimes():
        root = Path(rt.root_path)
        ok = root.is_dir() and os.access(root, os.R_OK | os.W_OK)
        if not ok:
            bad.append(f"{rt.key}({root})")
    if bad:
        return CheckResult("Runtime 目录", "fail", f"不可达或不可读写: {', '.join(bad)}")
    return CheckResult("Runtime 目录", "ok", "四端目录可达且可读写")


def _check_token(engine: LocalEngine) -> CheckResult:
    """token 检查（NFR-2）：P1 无云端连接，只检查无明文落盘。"""
    plaintext = engine.paths.repo_root.parent / "token.txt"
    candidates = [plaintext, engine.paths.repo_root / "token"]
    leaked = [str(p) for p in candidates if p.exists()]
    if leaked:
        return CheckResult("token", "fail", f"疑似明文 token 落盘: {', '.join(leaked)}")
    return CheckResult("token", "ok", "未配置云端 token（本地模式，P2 配对后生效）")


def _check_unmanaged_assets(engine: LocalEngine) -> CheckResult:
    """未纳管资产（FR-1.0.20）：统计四端 in_git=false 文件数与总大小（提示，不阻塞）。"""
    from skillos.core.algorithm.hash import ASSET_MAX_BYTES, list_content_files

    count = 0
    total = 0
    for rt in engine.db.list_runtimes():
        root = Path(rt.root_path)
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            try:
                for e in list_content_files(child):
                    if not e.in_git:
                        count += 1
                        total += (child / e.path).stat().st_size
            except OSError:
                continue  # 单个 skill 目录读不了不阻塞自检（push 时会显式报错）
    if count:
        mb = total / 1024 / 1024
        return CheckResult(
            "未纳管资产", "warn",
            f"{count} 个文件超 {ASSET_MAX_BYTES // 1024 // 1024}MB 未纳管，共 {mb:.1f}MB"
            "（分发时会原样保留，不参与漂移）",
        )
    return CheckResult("未纳管资产", "ok", "无超阈值资产")


def _check_git_identity(engine: LocalEngine) -> CheckResult:
    """第 10 项（防砸预案 10）：git user.name 未配置 → 时间轴作者显示 unknown，
    「谁在什么时候改了什么」的收尾叙事失去实物背书。演示机必须提前配置。
    """
    if not (engine.paths.repo_root / ".git").exists():
        return CheckResult("溯源身份", "warn", "仓库未初始化（先 skillos init）")
    name = subprocess.run(
        ["git", "-C", str(engine.paths.repo_root), "config", "user.name"],
        capture_output=True, timeout=10,
    ).stdout.decode().strip()
    if not name:
        return CheckResult(
            "溯源身份", "warn",
            "git user.name 未配置，时间轴作者将显示 unknown（防砸预案 10："
            "演示机执行 git config --global user.name <可读名字>）",
        )
    return CheckResult("溯源身份", "ok", f"user.name={name}")


def _check_logs(home: Path) -> CheckResult:
    """第 11 项（FR-1.1.11 / 04 §6.2.7）：日志目录可写 + 单文件大小 + 最近 ERROR 数。

    检查项：
    - `~/.skillos/logs/` 存在 + 当前用户可写
    - 当前 `<mode>.log` < 10MB（否则已轮转异常）
    - 最近 100 行内 ERROR / CRITICAL 数 > 10 → warn
    """
    log_dir = home / ".skillos" / "logs"
    if not log_dir.exists():
        return CheckResult(
            "日志系统", "warn",
            f"{log_dir} 不存在（首次运行 daemon / api / mcp 时创建）",
        )
    if not os.access(log_dir, os.R_OK | os.W_OK):
        return CheckResult(
            "日志系统", "fail",
            f"{log_dir} 不可读或不可写",
        )
    # 单文件大小（取 daemon.log 或最新）
    over_size: list[str] = []
    for p in sorted(log_dir.glob("*.log")):
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size >= 10 * 1024 * 1024:
            over_size.append(f"{p.name}={size // 1024 // 1024}MB")
    if over_size:
        return CheckResult(
            "日志系统", "fail",
            f"单文件超 10MB（轮转异常）: {', '.join(over_size)}",
        )
    # 最近 ERROR 数
    error_count = 0
    candidates = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime,
                        reverse=True)
    if candidates:
        target = candidates[0]
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()[-100:]
            for line in lines:
                # 兼容 JSON（"level":"ERROR"）和 key=value（level=ERROR）
                try:
                    rec = json.loads(line)
                    lv = str(rec.get("level", "")).upper()
                except (ValueError, TypeError):
                    lv = ""
                    for kv in line.split():
                        if kv.startswith("level="):
                            lv = kv.split("=", 1)[1].upper()
                            break
                if lv in ("ERROR", "CRITICAL"):
                    error_count += 1
        except OSError:
            pass
    if error_count > 10:
        return CheckResult(
            "日志系统", "warn",
            f"{target.name} 最近 100 行含 {error_count} 条 ERROR/CRITICAL",
        )
    return CheckResult(
        "日志系统", "ok",
        f"目录 {log_dir} 可写，文件大小正常"
        + (f"，最近 100 行 {error_count} 条 ERROR" if error_count else ""),
    )


def run_doctor(console: Console) -> int:
    """执行十一项检查并打印。返回 fail 数（>0 → CLI 退出码 1）。"""
    home = resolve_home()
    paths = default_env(home)
    engine = LocalEngine(paths, home=home)
    results = [
        _check_repo_location(paths.repo_root),
        _check_console_encoding(),
        _check_git(engine),
        _check_git_identity(engine),
        _check_lock(engine),
        _check_residue(engine),
        _check_wsl_boundary(engine),
        _check_runtime_dirs(engine),
        _check_token(engine),
        _check_unmanaged_assets(engine),
        _check_logs(home),  # 第 11 项：日志系统（T23 / 04 §6.2.7）
    ]
    fails = 0
    for r in results:
        style = _STYLE[r.level]
        console.print(f"[{style}]{_ICON[r.level]} {r.item}: {r.detail}[/{style}]")
        if r.level == "fail":
            fails += 1
    console.print(f"\n[bold]{'环境健康' if not fails else f'{fails} 项失败'}[/bold]")
    return fails
