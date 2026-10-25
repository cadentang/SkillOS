"""daemon 代理共用件（V2-M2）。

为什么需要它：N-02 报告、N-01.7/8 管理者视图、N-11.7 调用记录、V2-T1 开关
**全部是本地数据**。云端不碰磁盘（红线 5），所以一律经 `WS_TASK_OPS` 具名 op 向
daemon 取；错误统一成 `{ok:false, error:{code,message}}` 信封（HTTP 200），
前端按 `code` 分辨「设备离线」与「设备拒绝」，不用猜状态码。
"""

from __future__ import annotations

from fastapi import Request

from skillos.core.constants import WS_TASK_OPS


def ensure_task_op(op: str) -> None:
    """构造期白名单校验（OUT-14）：路由里出现未登记 op 直接炸，不静默下发。"""
    if op not in WS_TASK_OPS:
        raise RuntimeError(f"op 不在 WS_TASK_OPS 白名单: {op}")


async def call_daemon(request: Request, user_id: str, op: str, params: dict,
                      *, timeout: float = 30.0) -> dict:
    """下发具名任务并归一化结果（离线/失败都给信封，不抛 5xx 给前端猜）。"""
    ensure_task_op(op)
    hub = request.app.state.hub
    try:
        result = await hub.submit_task(user_id, op, params, timeout=timeout)
    except ConnectionError:
        return {
            "ok": False, "data": None,
            "error": {"code": "DAEMON_OFFLINE",
                      "message": "daemon 不在线，本机数据暂不可用"},
        }
    if not result.get("ok"):
        err = result.get("error") or {}
        return {
            "ok": False, "data": None,
            "error": {"code": err.get("code") or "INTERNAL",
                      "message": err.get("message") or ""},
        }
    return {"ok": True, "data": result.get("data"), "error": None}
