"""启动 SkillOS —— 后端 → 健康自证 → 配对 → daemon →（dev 时）前端。

硬约束（顺序不可乱）：
    后端起来 → 探到 /api/health 通 → 才配对 → 才起 daemon
为什么：daemon 依赖后端签发配对码；后端没起来就配对必然失败。

纪律（Agent 极易违反）：
    * **不做「执行了命令就算成功」** —— 一律以健康检查 / 进程存活为准
    * **幂等**：已在跑的就复用，不重复拉起
    * 拉起的常驻进程落 PID 到 state.json，由 stop.py 负责收

用法：
    python scripts/start.py                 # 默认单端口（后端托管 SPA）
    python scripts/start.py --dev           # 双端口：后端 + vite 热更
    python scripts/start.py --no-daemon     # 只起后端（只读视图）
    python scripts/start.py --open          # 起完打开浏览器
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402


def tail(path: Path, n: int = 30) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(无日志)"
    return "\n".join(lines[-n:]) or "(日志为空)"


def build_env(rt: dict, extra: dict | None = None) -> dict:
    """构造子进程环境：清掉沙盒变量、接上 vendor git、注入启动参数与 PYTHONPATH。"""
    env = os.environ.copy()
    for k in ("SKILLOS_ROOT", "SKILLOS_SANDBOX"):  # 本地启动一律回到 ~/.skillos
        env.pop(k, None)
    g = rt.get("git", {})
    if g.get("source") == "vendor":
        env["PATH"] = os.pathsep.join(g["bin_dirs"]) + os.pathsep + env.get("PATH", "")
        env["GIT_EXEC_PATH"] = g["exec_path"]
    # frozen 形态：skillos 不在 venv 里，只能靠 PYTHONPATH 找到编译产物
    pp = C.frozen_pythonpath()
    if pp:
        env["PYTHONPATH"] = pp + (os.pathsep + env["PYTHONPATH"]
                                  if env.get("PYTHONPATH") else "")
    for k, v in (extra or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    return env


def ensure_bootstrap(dev: bool, offline: bool) -> None:
    m = C.load_manifest()
    vpy = C.venv_python(C.venv_dir(m))
    need = not vpy.exists()
    if not need:
        # `-P`（isolated path，Python 3.11+）：不继承 cwd 到 sys.path。
        # 否则从仓库根运行会命中仓库里的 `skillos/` 源码 → 假阳性，
        # 导致明明没装却跳过 bootstrap。
        # **不能用 `-I`**：它隐含 `-E`，会连 PYTHONPATH 一起忽略 —— 而 frozen
        # 形态的 skillos 只能靠 PYTHONPATH 找到，那样会永远判"没就绪"。
        if C.is_frozen_mode(m):
            need = not deps_ready_public(vpy, m)
        else:
            p = subprocess.run([str(vpy), "-P", "-c", "import skillos"],
                               capture_output=True, text=True)
            need = p.returncode != 0
    if not need:
        return
    C.log("依赖未就绪，先执行 bootstrap …")
    argv = [sys.executable, str(Path(__file__).resolve().parent / "bootstrap.py")]
    if dev:
        argv.append("--dev")
    if offline:
        argv.append("--offline")
    rc = subprocess.run(argv).returncode
    if rc != 0:
        C.die("bootstrap 失败，已中止启动。")


def deps_ready_public(vpy: Path, m: dict) -> bool:
    """与 bootstrap.deps_ready 同口径（避免 import bootstrap 造成副作用）。"""
    env = os.environ.copy()
    pp = C.frozen_pythonpath(m)
    if pp:
        env["PYTHONPATH"] = pp
    p = subprocess.run([str(vpy), "-P", "-c",
                        "import skillos, fastapi, sqlalchemy, uvicorn, mcp"],
                       capture_output=True, text=True, env=env)
    return p.returncode == 0


def main() -> None:
    ap = argparse.ArgumentParser(description="启动 SkillOS（后端 + daemon + Web）")
    ap.add_argument("--dev", action="store_true", help="双端口：另起 vite 热更")
    ap.add_argument("--offline", action="store_true", help="禁外呼（透传给 bootstrap）")
    ap.add_argument("--no-daemon", action="store_true", help="只起后端，不起 daemon")
    ap.add_argument("--no-bootstrap", action="store_true", help="不自动装依赖")
    ap.add_argument("--open", action="store_true", help="起完打开浏览器")
    ap.add_argument("--host", default=None, help="覆盖监听地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=None, help="覆盖后端端口（默认 9080）")
    ap.add_argument("--email", default=None, help="配对账号（默认取 manifest.account）")
    ap.add_argument("--password", default=None, help="配对密码")
    args = ap.parse_args()

    C.setup_stdio()
    m = C.load_manifest()
    mode, repo = C.resolve_mode(m)
    host = args.host or m.get("host", "127.0.0.1")
    port = int(args.port or m.get("ports", {}).get("backend", 9080))
    web_port = int(m.get("ports", {}).get("web_dev", 5173))
    base = f"http://{host}:{port}"
    ws_url = f"ws://{host}:{port}/ws/agent"

    acct = m.get("account", {})
    email = args.email or os.environ.get("SKILLOS_ACCOUNT_EMAIL") or acct.get("email")
    password = (args.password or os.environ.get("SKILLOS_ACCOUNT_PASSWORD")
                or acct.get("password"))

    C.log(f"=== 启动 SkillOS · {mode} 模式 · {base} ===")

    # 二进制形态（docs/10）：运行时已编译进发行物，不需要 Python / venv / bootstrap。
    binary = C.is_binary_mode(m)
    frozen = C.is_frozen_mode(m)
    if binary:
        C.log("  运行形态：二进制（已编译；自足运行，不需要 Python / venv）")
    elif frozen:
        ext = C.frozen_extensions(m)
        pin = (m.get("runtimes", {}).get("python") or {}).get("pin")
        C.log(f"  运行形态：frozen（skillos 已编译：{', '.join(p.name for p in ext)}；"
              f"CPython {pin}）")
    if binary and args.dev:
        C.die("二进制形态不支持 --dev（编译产物无法热重载）。\n"
              "      → 要热更前端请用源码形态：仓库内 python scripts/start.py --dev")

    if not args.no_bootstrap and not binary:
        ensure_bootstrap(args.dev, args.offline)

    m = C.load_manifest()
    binary = C.is_binary_mode(m)
    frozen = C.is_frozen_mode(m)
    rt = C.resolve_runtimes(m, dev_mode=args.dev)
    venv = None
    vpy = None
    if not binary:
        venv = C.venv_dir(m)
        vpy = C.venv_python(venv)
        if not vpy.exists():
            C.die(f"venv 不存在（{vpy}）。先跑 python scripts/bootstrap.py")
    secret_file, _ = C.ensure_secret(m)
    secret = secret_file.read_text(encoding="utf-8").strip()
    db_path = C.expand(m["paths"]["cloud_db"])
    dist = C.web_dist_dir(repo)

    state = C.read_state(m)
    cwd = repo if repo else C.package_root()

    # ---------- 1) 后端（幂等）
    srv = state.get("server") or {}
    if C.health_ok(base):
        C.log(f"[1/4] 后端已在运行（健康检查通过），复用 {base}")
        if not srv.get("pid"):
            srv = {"pid": None, "host": host, "port": port, "url": base,
                   "started_at": srv.get("started_at") or C.now_iso(), "adopted": True}
    else:
        if C.port_open(host, port):
            C.die(f"端口 {port} 已被占用，但 /api/health 不通。\n"
                  f"      → 可能是残留进程或别的服务。先 `python scripts/stop.py`，"
                  f"或换端口 `--port 9081`。")
        extra = {
            "SKILLOS_JWT_SECRET": secret,
            "SKILLOS_DB_URL": f"sqlite:///{db_path.as_posix()}",
            "SKILLOS_WEB_DIST": None if args.dev else (str(dist) if dist else None),
        }
        if not args.dev and not dist:
            C.log("  注意：未找到 web/dist，后端将不托管 SPA（可用 --dev 或先 bootstrap）。")
        if binary:
            # 编译入口读环境变量：uvicorn CLI 的字符串入口在产物里不可用（docs/10 §4）
            extra["SKILLOS_HOST"] = host
            extra["SKILLOS_PORT"] = str(port)
        env = build_env(rt, extra)
        if binary:
            argv = C.binary_backend_argv(m, host, port)
        else:
            argv = C.backend_argv(venv, host, port,
                                  reload=args.dev and bool(repo),
                                  reload_dir=str(repo / "skillos") if repo else None)
        logp = C.logs_dir(m) / "backend.log"
        C.log(f"[1/4] 启动后端：{' '.join(str(a) for a in argv)}")
        try:
            pid = C.spawn_detached(argv, cwd=cwd, env=env, log_path=logp)
        except OSError as e:
            hint = ("检查发行目录是否完整（skillos/ 下的二进制是否被误删）"
                    if binary else f"检查 venv 是否完好：{vpy}")
            C.die(f"拉起后端失败：{e}\n      → {hint}")
        if not C.wait_health(base, seconds=45):
            C.log("  后端未通过健康检查，日志尾部：")
            C.log("  " + "\n  ".join(tail(logp).splitlines()))
            C.die("后端启动失败，已中止（进程可能仍在，用 stop.py 收掉）。")
        srv = {"pid": pid, "host": host, "port": port, "url": base,
               "started_at": C.now_iso(), "dev": bool(args.dev)}
        C.log(f"  ✓ 后端就绪（pid {pid}）")

    # ---------- 2) 配对（token 已在则跳过）
    token_file = C.daemon_token_path()
    pair_code = None
    pairing = "existing"
    if token_file.exists():
        C.log(f"[2/4] 已配对（token 存在：{token_file}），跳过")
    else:
        C.log(f"[2/4] 申请配对码（账号 {email}）…")
        try:
            pair_code = C.pairing_code(base, email, password)
            pairing = "new"
            C.log(f"  ✓ 取得配对码 {pair_code}")
        except Exception as e:  # noqa: BLE001
            pairing = f"failed:{e}"
            C.log(f"  ✗ 配对失败：{e}")

    # ---------- 3) daemon
    dmon = state.get("daemon") or {}
    if args.no_daemon:
        C.log("[3/4] 按参数跳过 daemon（--no-daemon，Web 为只读视图）")
        dmon = {}
    elif C.pid_alive(dmon.get("pid")):
        C.log(f"[3/4] daemon 已在运行（pid {dmon.get('pid')}），复用")
    elif token_file.exists() or pair_code:
        env = build_env(rt)
        argv = (C.binary_daemon_argv(m, ws_url, pair_code) if binary
                else C.daemon_argv(venv, ws_url, pair_code))
        logp = C.logs_dir(m) / "daemon.log"
        C.log(f"[3/4] 启动 daemon：{' '.join(str(a) for a in argv)}")
        try:
            pid = C.spawn_detached(argv, cwd=cwd, env=env, log_path=logp)
        except OSError as e:
            pid = None
            C.log(f"  ✗ 拉起 daemon 失败：{e}")
        else:
            time.sleep(1.5)
            if C.pid_alive(pid):
                dmon = {"pid": pid, "cloud": ws_url, "started_at": C.now_iso()}
                C.log(f"  ✓ daemon 已拉起（pid {pid}）")
            else:
                pid = None
                C.log("  ✗ daemon 拉起后立即退出，日志尾部：")
                C.log("  " + "\n  ".join(tail(logp, 15).splitlines()))
        if pid is None:
            dmon = {}
    else:
        C.log("[3/4] 无 token 且配对失败 —— 跳过 daemon（Web 为只读视图）")
        dmon = {}

    # ---------- 4) 前端（仅 dev）
    webd = state.get("web_dev") or {}
    if args.dev:
        if C.pid_alive(webd.get("pid")):
            C.log(f"[4/4] vite 已在运行（pid {webd.get('pid')}），复用")
        else:
            src = C.web_src_dir(repo)
            if not src:
                C.log("[4/4] dev 模式但找不到前端源码 —— 跳过 vite")
            elif C.port_open(host, web_port):
                C.log(f"[4/4] 端口 {web_port} 已占用 —— 跳过 vite")
            else:
                logp = C.logs_dir(m) / "web.log"
                C.log(f"[4/4] 启动 vite：{' '.join(C.npm_argv(['run', 'dev']))} @ {src}")
                try:
                    pid = C.spawn_detached(C.npm_argv(["run", "dev"]), cwd=src,
                                           env=build_env(rt), log_path=logp)
                except OSError as e:
                    pid = None
                    C.log(f"  ✗ 拉起 vite 失败：{e}")
                else:
                    time.sleep(3.0)
                    if C.pid_alive(pid):
                        webd = {"pid": pid, "port": web_port,
                                "url": f"http://{host}:{web_port}",
                                "started_at": C.now_iso()}
                        C.log(f"  ✓ vite 已拉起（pid {pid}）")
                    else:
                        pid = None
                        C.log("  ✗ vite 拉起后退出，日志尾部：")
                        C.log("  " + "\n  ".join(tail(logp, 15).splitlines()))
                if pid is None:
                    webd = {}
    else:
        C.log("[4/4] 单端口模式：SPA 由后端托管")
        webd = {}

    # ---------- 落状态 + 汇总
    state.update({
        "launcher_version": m.get("launcher_version"),
        "mode": mode,
        "binary": binary,
        "frozen": frozen,
        "platform": rt["platform"],
        "runtimes": {"python": rt["python"], "git": rt["git"], "node": rt["node"]},
        "server": srv,
        "daemon": dmon,
        "web_dev": webd,
        "db_url": f"sqlite:///{db_path.as_posix()}",
        "updated_at": C.now_iso(),
    })
    C.write_state(state, m)

    ui_url = f"http://{host}:{web_port}" if args.dev else base
    C.log("")
    C.log("SkillOS 已启动：")
    C.log(f"  控制台   {ui_url}")
    C.log(f"  后端 API {base}")
    C.log(f"  daemon   {ws_url}   ({'运行中' if dmon.get('pid') else '未运行'})")
    C.log(f"  配对     {pairing}")
    C.log(f"  日志     {C.logs_dir(m)}")
    if args.open:
        try:
            webbrowser.open(ui_url)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"start: {e}", file=sys.stderr)
        sys.exit(1)
