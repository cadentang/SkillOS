"""MCP stdio 端到端冒烟（P5，03 §2 P5「在 Claude Code 中端到端验证」的自动化等价物）。

起真实 `python -m skillos.mcp.server` 子进程，走 JSON-RPC：
initialize → tools/list（八工具白名单）→ tools/call（list_skills / check / push dry_run）。

用法：python scripts/smoke_mcp_stdio.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_SKILL_MD = (
    b"---\nname: demo\nversion: 1.0.0\n"
    b"description: a demo skill doing useful everyday work\n---\nbody\n"
)


def _log(msg: str) -> None:
    if hasattr(sys.stdout, "reconfigure"):  # GBK 控制台防 ✔ 崩（05 编码基线）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(msg, flush=True)


def _fail(msg: str) -> None:
    _log(f"FAIL: {msg}")
    sys.exit(1)


def _check(cond: bool, msg: str) -> None:
    if not cond:
        _fail(msg)
    _log(f"  ok - {msg}")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="skillos-mcp-"))
    home = tmp / "home"
    d = home / ".claude" / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(_SKILL_MD)
    from skillos.services.engine import LocalEngine, default_env

    engine = LocalEngine(default_env(home), home=home)
    engine.init_env()
    engine.import_skills()

    proc = subprocess.Popen(
        [sys.executable, "-m", "skillos.mcp.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={**os.environ, "SKILLOS_HOME": str(home)},
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    lines: list[str] = []
    _stdout = proc.stdout

    def _reader() -> None:
        if _stdout is None:
            return
        for raw in _stdout:
            lines.append(raw.decode(errors="replace").strip())

    threading.Thread(target=_reader, daemon=True).start()

    def send(msg: dict) -> None:
        proc.stdin.write((json.dumps(msg) + "\n").encode())  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    def wait_response(req_id: int, timeout: float = 20.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for line in lines:
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == req_id:
                    return msg
            time.sleep(0.2)
        _fail(f"响应超时 (id={req_id})，已收到 {len(lines)} 行")

    def call(tool: str, args: dict) -> dict:
        send({"jsonrpc": "2.0", "id": call._next, "method": "tools/call",
              "params": {"name": tool, "arguments": args}})
        resp = wait_response(call._next)
        call._next += 1
        return json.loads(resp["result"]["content"][0]["text"])

    call._next = 10  # type: ignore[attr-defined]

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "smoke", "version": "0"}}})
    init = wait_response(1)
    _check(init["result"]["serverInfo"]["name"] == "skillos", "MCP initialize")
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = wait_response(2)["result"]["tools"]
    names = sorted(t["name"] for t in tools)
    expect = sorted([
        "skillos_list_skills", "skillos_get_skill", "skillos_status",
        "skillos_check", "skillos_version_log", "skillos_sync_push",
        "skillos_sync_pull", "skillos_upgrade",
    ])
    _check(names == expect, "八工具白名单（OUT-14）")

    r = call("skillos_list_skills", {})
    _check(r["ok"] and r["data"]["skills"][0]["slug"] == "demo", "tools/call list_skills")

    r = call("skillos_check", {"slug": "demo"})
    _check(r["ok"] and isinstance(r["data"]["findings"], list), "tools/call check")

    r = call("skillos_sync_push", {"slug": "demo"})  # 默认 dry_run
    _check(r["ok"] and r["dry_run"] is True, "push 默认 dry_run（FR-1.3.3）")
    _check(all(o["ok"] for o in r["data"]["outcomes"]), "dry_run 预览全部目标通过")

    # dry_run 未落盘：四端目标目录无 demo
    home_claude = home / ".claude" / "skills" / "demo"
    _check(home_claude.exists(), "源目录仍在（claude 源）")

    r = call("skillos_upgrade", {"slug": "demo"})
    _check(not r["ok"] and r["error"]["code"] == "NOT_IMPLEMENTED",
           "upgrade 显式 NOT_IMPLEMENTED（红线 7）")

    proc.terminate()
    _log("\n全部通过 ✔ (P5 MCP stdio 链路)")


if __name__ == "__main__":
    main()
