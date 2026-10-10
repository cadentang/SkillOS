"""只读总览 —— 进程 / 端口 / 健康 / 运行时来源一览。不改任何东西。

用法：
    python scripts/status.py
    python scripts/status.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402


def collect() -> dict:
    m = C.load_manifest()
    mode, repo = C.resolve_mode(m)
    ports = m.get("ports", {})
    host = m.get("host", "127.0.0.1")
    bport = int(ports.get("backend", 9080))
    wport = int(ports.get("web_dev", 5173))
    base = f"http://{host}:{bport}"

    state = C.read_state(m)
    srv = state.get("server") or {}
    dmon = state.get("daemon") or {}
    webd = state.get("web_dev") or {}

    # 端口以「实际记录的」为准（start.py 可能用 --port 覆盖过 manifest 默认值）
    if srv.get("port"):
        bport = int(srv["port"])
        base = srv.get("url") or f"http://{host}:{bport}"
    if webd.get("port"):
        wport = int(webd["port"])

    health = C.health(base)
    token = C.daemon_token_path()

    def probe(entry: dict, port: int | None) -> dict:
        pid = entry.get("pid")
        return {
            "pid": pid,
            "alive": C.pid_alive(pid),
            "port": port,
            "port_open": C.port_open(host, port) if port else None,
            "url": entry.get("url"),
            "started_at": entry.get("started_at"),
        }

    return {
        "launcher_version": m.get("launcher_version"),
        "mode": mode,
        "platform": C.platform_key(),
        "backend": {**probe(srv, bport), "health": health,
                    "healthy": bool(health) and health.get("status") == "up",
                    "base": base},
        "daemon": {**probe(dmon, None), "cloud": dmon.get("cloud"),
                   "token_file": str(token), "token_present": token.exists()},
        "web_dev": probe(webd, wport),
        "runtime_source": (state.get("runtimes") or {}).get("python", {}).get("source"),
        "state_file": str(C.state_file(m)),
    }


def human(r: dict) -> str:
    def dot(ok) -> str:
        return "✓" if ok else "✗"

    b, d, w = r["backend"], r["daemon"], r["web_dev"]
    lines = [
        f"=== SkillOS 状态 · {r['platform']} · {r['mode']} ===",
        "",
        f"后端    {dot(b['healthy'])}  {b['base']}  "
        f"(pid {b['pid'] or '—'}, 端口 {b['port']} "
        f"{'占用' if b['port_open'] else '空闲'}"
        f"{', sandbox' if (b['health'] or {}).get('sandbox') else ''})",
        f"daemon  {dot(d['alive'])}  {d['cloud'] or '—'}  "
        f"(pid {d['pid'] or '—'}, token {'已落盘' if d['token_present'] else '未配对'})",
    ]
    if w["pid"] or w["port_open"]:
        lines.append(f"web(dev){dot(w['alive'])}  {w['url'] or '—'}  "
                     f"(pid {w['pid'] or '—'}, 端口 {w['port']})")
    lines += [
        "",
        f"python 来源 : {r['runtime_source'] or '（尚未记录，跑一次 start 后写入）'}",
        f"状态文件   : {r['state_file']}",
    ]
    if not b["healthy"] and not d["alive"]:
        lines += ["", "提示：未在运行。执行 `python scripts/start.py` 启动。"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="只读查看 SkillOS 运行状态")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    C.setup_stdio()
    r = collect()
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else human(r))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"status: {e}", file=sys.stderr)
        sys.exit(1)
