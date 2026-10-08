"""P2 Web 安全线端到端冒烟：真 FastAPI + 真 daemon（沙盒 home）。

验证链路：register/login → 配对 → live 漂移矩阵（徽标 + manager 统计）→
一键分发（FR-2.1.4 逐项）→ log 时间轴 → gate-summary。
跑法：python scripts/smoke_web_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import uvicorn


def _log(msg: str) -> None:
    print(msg, flush=True)

BASE = "http://127.0.0.1:8765"
SKILL_MD = (
    b"---\nname: demo\nversion: 1.0.0\n"
    b"description: smoke test skill for e2e runs fine\n---\nbody\n"
)


def _fail(msg: str) -> None:
    _log(f"FAIL: {msg}")
    os._exit(1)


def _check(cond: bool, msg: str) -> None:
    if not cond:
        _fail(msg)
    _log(f"  ok - {msg}")


async def main() -> None:
    from skillos.api.main import create_app
    from skillos.daemon.main import run as daemon_run

    tmp = Path(tempfile.mkdtemp(prefix="skillos-e2e-"))
    home = tmp / "home"
    d = home / ".claude" / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(SKILL_MD)

    # 本地环境预初始化（等价 CLI: skillos init && skillos import）
    from skillos.services.engine import LocalEngine, default_env

    eng = LocalEngine(default_env(home), home=home)
    eng.init_env()
    eng.import_skills()

    app = create_app(db_url=f"sqlite:///{tmp / 'cloud.db'}", jwt_secret="e2e-secret")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    _check(server.started, "云端启动")

    c = httpx.Client(base_url=BASE, timeout=30)

    # ---- 注册 / 登录 ----
    r = c.post("/api/auth/register", json={"email": "demo@example.com", "password": "password123"})
    _check(r.status_code == 200, "注册")
    r = c.post("/api/auth/login", json={"email": "demo@example.com", "password": "password123"})
    _check(r.status_code == 200, "登录")
    token = r.json()["data"]["token"]
    auth = {"Authorization": f"Bearer {token}"}

    # ---- 配对 daemon ----
    code = c.post("/api/pairing/code", headers=auth).json()["data"]["code"]
    # daemon 线程必须 daemon=True：其内部 while True 重连循环不随主流程退出
    threading.Thread(
        target=lambda: asyncio.run(daemon_run("ws://127.0.0.1:8765/ws/agent", code, home)),
        daemon=True,
    ).start()
    await asyncio.sleep(3.0)  # daemon 握手 + 首次漂移上报

    # ---- live 漂移矩阵 ----
    r = c.get("/api/sync/status?live=1", headers=auth)
    _check(r.status_code == 200, "GET /api/sync/status")
    data = r.json()["data"]
    _check(data["daemon_online"] is True and data["stale"] is False, "live 矩阵（daemon 在线）")
    slugs = {row["slug"] for row in data["rows"]}
    _check("demo" in slugs, "矩阵含 demo")
    demo = next(row for row in data["rows"] if row["slug"] == "demo")
    _check("claude" in demo["cells"], "demo 有 claude 格")
    _check(demo["state"] in ("SYNCED", "PARTIAL", "OUTDATED", "MODIFIED", "UNMANAGED"),
           f"demo 聚合态={demo['state']}（已知状态）")
    _check(data["manager"]["total_skills"] >= 1, "manager 统计")

    # ---- 一键分发 ----
    r = c.post("/api/sync/push", headers=auth, json={"skill_ids": ["demo"]})
    body = r.json()
    _check(r.status_code == 200, "POST /api/sync/push")
    _check(body["ok"] and all(x["ok"] for x in body["data"]["results"]), "demo 分发成功")

    # ---- 分发后矩阵收敛 SYNCED ----
    await asyncio.sleep(1.0)
    r = c.get("/api/sync/status?live=1", headers=auth)
    data = r.json()["data"]
    demo = next((row for row in data["rows"] if row["slug"] == "demo"), None)
    state = demo["state"] if demo else None
    _check(demo is not None and state == "SYNCED", f"分发后 demo 聚合态={state}")

    # ---- log 时间轴 ----
    r = c.get("/api/skills/demo/log", headers=auth)
    body = r.json()
    _check(r.status_code == 200 and body["ok"], "GET /api/skills/demo/log")
    items = body["data"]["items"]
    _check(len(items) >= 1 and all("sha" in i and "at" in i for i in items),
           f"时间轴 {len(items)} 条提交")

    # ---- gate-summary ----
    r = c.post("/api/sync/gate-summary", headers=auth)
    body = r.json()
    _check(r.status_code == 200 and body["ok"], "POST /api/sync/gate-summary")
    _check("demo" in body["data"]["summary"], "门禁计数含 demo")

    # ---- P3：文件树 / 读取 / STALE 乐观锁 / 写入 / checks / diff ----
    r = c.get("/api/skills/demo/files", headers=auth)
    body = r.json()
    _check(r.status_code == 200 and body["ok"], "GET /api/skills/demo/files")
    tree = body["data"]["tree"]
    _check(any(f["path"] == "SKILL.md" for f in tree), "文件树含 SKILL.md")

    r = c.get("/api/skills/demo/files/content", headers=auth, params={"path": "SKILL.md"})
    body = r.json()
    _check(r.status_code == 200 and body["ok"], "GET files/content")
    base_hash = body["data"]["hash"]
    original = body["data"]["content"]

    r = c.post("/api/skills/demo/files", headers=auth, json={
        "path": "SKILL.md", "content": original, "base_hash": "0" * 64, "message": "stale",
    })
    _check(r.status_code == 409 and "STALE" in r.text, "409 STALE（乐观锁拒覆盖）")

    edited = original.replace("smoke test skill for e2e runs fine",
                              "smoke test skill for e2e runs fine, edited by web")
    r = c.post("/api/skills/demo/files", headers=auth, json={
        "path": "SKILL.md", "content": edited, "base_hash": base_hash, "message": "web edit",
    })
    body = r.json()
    _check(r.status_code == 200 and body["ok"], "POST files（合法写）")
    _check(len(body["data"]["commit"]) >= 7, f"新 commit {body['data']['commit'][:10]}")
    _check(isinstance(body["data"]["gate"], list), "返回逐条门禁 findings")

    r = c.get("/api/skills/demo/checks", headers=auth)
    body = r.json()
    _check(r.status_code == 200 and body["ok"], "GET /api/skills/demo/checks")
    _check(isinstance(body["data"]["findings"], list), "逐条 findings")
    _check(body["data"]["decay"] is not None
           and body["data"]["decay"]["staleness_days"] is not None, "折旧度量")

    r = c.get("/api/skills/demo/log", headers=auth)
    shas = [i["sha"] for i in r.json()["data"]["items"]]
    _check(len(shas) >= 2, f"log {len(shas)} 条（含编辑 commit）")
    r = c.get("/api/skills/demo/diff", headers=auth,
              params={"base": shas[1], "head": shas[0]})
    body = r.json()
    _check(r.status_code == 200 and body["ok"], "GET /api/skills/demo/diff")
    added = [ln for ln in body["data"]["unified"].splitlines()
             if ln.startswith("+") and "edited by web" in ln]
    _check(bool(added), "diff 含编辑行")

    _log("\n全部通过 ✔ (P2 安全线 + P3 写链路)")
    os._exit(0)  # uvicorn / daemon 线程随进程硬退出


if __name__ == "__main__":
    asyncio.run(main())
