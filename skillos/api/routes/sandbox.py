"""沙盒 API（FR-3.10 / FR-2.4.3 / E-06.4，P7 → M5）。

- `GET /api/health` 的 sandbox 字段由 create_app 设置（SKILLOS_SANDBOX=1）
- `POST /api/sandbox/reset`：仅沙盒模式存在（否则 404），经 hub 下发
  `sandbox_reset` 任务给沙盒 daemon —— 云端不碰磁盘（红线 5），
  文件恢复与 seed 自证全部在容器内 daemon 侧执行
- 重置是**长任务**（清空四个 deploy 根 + 重建 repo + 全量 push + 自证），
  同步等会撞网关超时 ⇒ 回 `202 Accepted` + `task_id`（E-06.4），
  进度用 `GET /api/sandbox/reset/{task_id}` 轮询。
"""

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from skillos.api.deps import get_current_user_id

router = APIRouter(tags=["sandbox"])

_RESET_TIMEOUT_S = 300.0


def _tasks(request: Request) -> dict:
    """进程内任务表（重置是演示态操作，不落库：重启后"进行中"本就无意义）。"""
    tasks = getattr(request.app.state, "sandbox_tasks", None)
    if tasks is None:
        tasks = {}
        request.app.state.sandbox_tasks = tasks
    return tasks


def _daemon_online(request: Request, user_id: str) -> bool:
    hub = request.app.state.hub
    return any(a.user_id == user_id for a in hub.agents.values())


@router.post("/sandbox/reset")
async def sandbox_reset(request: Request,
                        user_id: str = Depends(get_current_user_id)) -> JSONResponse:
    """恢复预置演示数据（评委可反复体验的前提）。

    非沙盒部署此路由不存在（create_app 不挂载）；沙盒下 daemon 离线 → 503；
    在线 → `202 {task_id}`（幂等：同一时刻只允许一个重置在跑）。
    """
    if not getattr(request.app.state, "sandbox", False):
        raise HTTPException(status_code=404, detail="NOT_SANDBOX")
    if not _daemon_online(request, user_id):
        # 离线在**入口**就拒：否则 202 之后永远停在 running，前端只能靠超时猜
        raise HTTPException(status_code=503, detail="DAEMON_OFFLINE")

    tasks = _tasks(request)
    running = next(
        (tid for tid, t in tasks.items() if t.get("status") == "running"), None,
    )
    if running is not None:
        return JSONResponse(status_code=202, content={
            "ok": True,
            "data": {"task_id": running, "status": "running", "reused": True},
            "error": None,
        })

    task_id = f"sbx_{secrets.token_hex(6)}"
    tasks[task_id] = {"status": "running", "result": None, "error": None}

    async def _run() -> None:
        try:
            result = await request.app.state.hub.submit_task(
                user_id, "sandbox_reset", {}, timeout=_RESET_TIMEOUT_S,
            )
        except ConnectionError:
            tasks[task_id].update(status="failed", error="DAEMON_OFFLINE")
            return
        except Exception as e:  # noqa: BLE001 — 任务态必须收敛，不能永远 running
            tasks[task_id].update(status="failed", error=str(e))
            return
        if not result.get("ok"):
            err = result.get("error") or {}
            tasks[task_id].update(
                status="failed", error=err.get("message") or "RESET_FAILED",
                code=err.get("code"),
            )
            return
        tasks[task_id].update(status="succeeded", result=result.get("data"))

    asyncio.create_task(_run())
    return JSONResponse(status_code=202, content={
        "ok": True,
        "data": {"task_id": task_id, "status": "running"},
        "error": None,
    })


@router.get("/sandbox/reset/{task_id}")
def sandbox_reset_status(task_id: str, request: Request,
                         user_id: str = Depends(get_current_user_id)) -> dict:
    """重置进度（E-06.4：前端显示"进行中"而不是转圈等一个可能超时的请求）。"""
    if not getattr(request.app.state, "sandbox", False):
        raise HTTPException(status_code=404, detail="NOT_SANDBOX")
    task = _tasks(request).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")
    return {"ok": True, "data": {"task_id": task_id, **task}, "error": None}
