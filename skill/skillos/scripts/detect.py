"""只读环境探测 —— 不改任何文件、不起任何进程。

用途：
    * Agent / 用户在执行 bootstrap / start 前先看清「本机有什么、缺什么」
    * 让「检测优先」策略可被审计（用的到底是系统运行时还是 vendor 兜底）

用法：
    python scripts/detect.py            # 人类可读摘要
    python scripts/detect.py --json     # 机器可读（Agent 用这个）
    python scripts/detect.py --dev      # 按 dev 模式口径（会把 node 纳入必需）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402


def build_report(dev_mode: bool) -> dict:
    m = C.load_manifest()
    rt = C.resolve_runtimes(m, dev_mode=dev_mode)
    mode, repo = C.resolve_mode(m)
    binary = C.is_binary_mode(m)
    frozen = C.is_frozen_mode(m)
    venv = C.venv_dir(m)
    dist = C.web_dist_dir(repo)
    return {
        "launcher_version": m.get("launcher_version"),
        "skillos_version": m.get("skillos_version"),
        "protocol_version": m.get("protocol_version"),
        "mode": mode,
        "binary_mode": binary,
        "frozen_mode": frozen,
        "repo_root": str(repo) if repo else None,
        "platform": rt["platform"],
        "runtimes": {
            "python": rt["python"],
            "git": rt["git"],
            "node": rt["node"],
        },
        "notes": rt["notes"],
        "venv": {"path": str(venv), "ready": C.venv_python(venv).exists(),
                 "required": not binary},
        "binary_roles": C.binary_roles(m),
        "frozen": {
            "dir": str(C.frozen_dir(m)) if C.frozen_dir(m) else None,
            "extensions": [p.name for p in C.frozen_extensions(m)],
            "requirements": (str(C.frozen_requirements(m))
                             if C.frozen_requirements(m) else None),
            "vendored_python": (str(C.frozen_runtime_python(m))
                                if C.frozen_runtime_python(m) else None),
        },
        "web_dist": str(dist) if dist else None,
        "web_src": str(C.web_src_dir(repo)) if C.web_src_dir(repo) else None,
        "state_dir": str(C.state_dir(m)),
        "host": m.get("host", "127.0.0.1"),
        "ports": m.get("ports", {}),
    }


def human(report: dict) -> str:
    r = report
    py, git, node = r["runtimes"]["python"], r["runtimes"]["git"], r["runtimes"]["node"]

    def fmt(d: dict) -> str:
        src = d.get("source", "?")
        if src == "missing":
            return "缺失 ✗"
        bits = [src]
        if d.get("version"):
            bits.append(str(d["version"]))
        if d.get("path"):
            bits.append(f"[{d['path']}]")
        elif d.get("argv"):
            bits.append(f"[{' '.join(d['argv'])}]")
        elif d.get("reason"):
            bits.append(f"({d['reason']})")
        return "  ".join(bits)

    if r["binary_mode"]:
        shape = "二进制（已编译，自足）"
    elif r["frozen_mode"]:
        shape = "frozen（skillos 已编译，依赖走 venv）"
    else:
        shape = "源码（需要 Python）"
    lines = [
        f"=== 环境探测 · {r['platform']} ===",
        f"发行形态   : {r['mode']} —— {shape}"
        + (f"  ({r['repo_root']})" if r["repo_root"] else ""),
        f"launcher   : {r['launcher_version']}  skillos {r['skillos_version']}"
        f"  协议 v{r['protocol_version']}",
        f"python     : {fmt(py)}",
        f"git        : {fmt(git)}",
        f"node       : {fmt(node)}",
    ]
    if r["binary_mode"]:
        lines.append("可执行文件 :")
        for role, info in r["binary_roles"].items():
            lines.append(f"  {'✓' if info['available'] else '✗'} {role:<7} {info['path']}")
    else:
        lines.append(
            f"venv       : {'就绪' if r['venv']['ready'] else '未创建'}  ({r['venv']['path']})")
    if r["frozen_mode"]:
        fz = r["frozen"]
        lines += [
            f"编译产物   : {', '.join(fz['extensions']) or '（未找到）'}  @ {fz['dir']}",
            f"依赖清单   : {fz['requirements'] or '（缺）'}",
            f"便携运行时 : {fz['vendored_python'] or '未随包（需系统装同版本 Python）'}",
        ]
    lines += [
        f"web 构建产物: {r['web_dist'] or '未构建'}",
        f"状态目录   : {r['state_dir']}",
    ]
    if r["notes"]:
        lines.append("备注:")
        lines += [f"  - {n}" for n in r["notes"]]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="只读探测本机运行时与发行形态")
    ap.add_argument("--json", action="store_true", help="输出 JSON（Agent 友好）")
    ap.add_argument("--dev", action="store_true", help="按 dev 口径把 node 纳入必需")
    args = ap.parse_args()

    C.setup_stdio()
    report = build_report(args.dev)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(human(report))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"detect: {e}", file=sys.stderr)
        sys.exit(1)
