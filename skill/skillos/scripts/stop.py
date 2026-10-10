"""停止 SkillOS —— 按 state.json 记录的 PID 优雅停止（daemon → web → 后端）。

用法：
    python scripts/stop.py
    python scripts/stop.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402

# 停止顺序：先断依赖后端的进程，最后收后端
_ORDER = (("daemon", "daemon"), ("web_dev", "vite"), ("server", "后端"))


def main() -> None:
    ap = argparse.ArgumentParser(description="停止 SkillOS（按 PID 优雅停止）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    C.setup_stdio()

    m = C.load_manifest()
    host = m.get("host", "127.0.0.1")
    bport = int(m.get("ports", {}).get("backend", 9080))
    state = C.read_state(m)
    report = []

    for key, label in _ORDER:
        entry = state.get(key) or {}
        pid = entry.get("pid")
        if not pid:
            report.append({"component": key, "result": "no-pid"})
            continue
        if not C.pid_alive(pid):
            report.append({"component": key, "pid": pid, "result": "already-gone"})
            entry["pid"] = None
            state[key] = entry
            continue
        C.log(f"停止 {label} (pid {pid}) …")
        ok = C.terminate_pid(pid)
        report.append({"component": key, "pid": pid,
                       "result": "stopped" if ok else "failed"})
        C.log(f"  {'✓ 已停止' if ok else '✗ 未能停止（可能需手动）'}")
        entry["pid"] = None
        state[key] = entry

    state["stopped_at"] = C.now_iso()
    C.write_state(state, m)

    # 收尾自证：后端端口应已释放
    still = C.port_open(host, bport)
    if still:
        C.log(f"警告：端口 {bport} 仍被占用（可能有非本工具拉起的进程）。")

    if args.json:
        print(json.dumps({"report": report, "backend_port_free": not still},
                         ensure_ascii=False, indent=2))
    else:
        C.log("已停止。" if not still else "已停止（但后端端口仍占用）。")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"stop: {e}", file=sys.stderr)
        sys.exit(1)
