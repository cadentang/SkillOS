"""WebSocket 连接管理（FR-3.8，04 §4.2）。

两个通道：
- /ws/agent：daemon 出站连接（hello / task / result / event / ping-pong）
- /ws/ui：前端订阅（task_progress / task_done / drift_changed / daemon_status）

断连语义（04 §4.2）：断连不等于失败 —— 任务未确认完成落 unknown，
重连后补发未 ack 的 result（按 request_id 去重），前端进度条冻结不伪造成败。

request_id 守卫（07 N12）：无 request_id 的任务直接拒绝；
云端对 result 按 request_id 去重消费，不按 task_id。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field

from fastapi import WebSocket

log = logging.getLogger("skillos.ws")

# 已消费 request_id 的 LRU 窗口（07 N12 守卫点 ①）
_CONSUMED_WINDOW = 512


@dataclass
class AgentConn:
    ws: WebSocket
    agent_id: str
    user_id: str
    token_id: str
    connected_at: float = field(default_factory=time.time)


@dataclass
class PendingTask:
    request_id: str
    task_id: str
    user_id: str  # task_done 按 user 定向广播（不跨用户泄漏进度）
    op: str
    params: dict
    future: asyncio.Future
    submitted_at: float = field(default_factory=time.time)


class WsHub:
    """连接注册表 + 通道隔离 + 任务编排。

    单进程内存态：云端与 daemon 的会话状态不落盘（任务结果落 sync_records 审计）。
    """

    def __init__(self) -> None:
        # agent_id → AgentConn（FR-1.2.7：同 agent_id 唯一，新连接踢旧连接）
        self.agents: dict[str, AgentConn] = {}
        # request_id → PendingTask（等待 result）
        self.pending: dict[str, PendingTask] = {}
        # 已消费 request_id 去重（重连补发场景）
        self._consumed: dict[str, float] = {}
        # UI 连接：ws → user_id（daemon_status / task_done / drift_changed
        # 都是用户维度的状态，跨用户广播会把 A 的 daemon 在线态点亮到 B 的徽标）
        self.ui_conns: dict[WebSocket, str] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- agent 通道

    async def register_agent(self, conn: AgentConn) -> AgentConn | None:
        """注册 agent 连接。返回被踢掉的旧连接（如有）。"""
        async with self._lock:
            old = self.agents.get(conn.agent_id)
            self.agents[conn.agent_id] = conn
        if old is not None:
            try:
                await old.ws.close(code=4001, reason="superseded by new connection")
            except Exception:  # noqa: BLE001 —— 关闭已死连接属常态
                pass
        await self.broadcast_ui({"type": "daemon_status", "connected": True,
                                 "agent_id": conn.agent_id},
                                user_id=conn.user_id)
        return old

    async def unregister_agent(self, conn: AgentConn) -> None:
        async with self._lock:
            cur = self.agents.get(conn.agent_id)
            if cur is conn:
                del self.agents[conn.agent_id]
                # 断连 ≠ 失败（04 §4.2）：未完成任务落 unknown，不判 failed
                for req_id, task in list(self.pending.items()):
                    if not task.future.done():
                        task.future.set_result({
                            "ok": False, "error": {"code": "UNKNOWN",
                                                   "message": "daemon 断连，任务结果未知"},
                        })
                        del self.pending[req_id]
        await self.broadcast_ui({"type": "daemon_status", "connected": False,
                                 "agent_id": conn.agent_id},
                                user_id=conn.user_id)

    def agent_online(self, user_id: str) -> bool:
        return any(a.user_id == user_id for a in self.agents.values())

    async def handle_result(self, request_id: str, payload: dict) -> None:
        """daemon result 处理：按 request_id 去重消费。"""
        if request_id in self._consumed:
            log.info("重复 result 忽略（request_id=%s 已消费）", request_id)
            return
        self._consumed[request_id] = time.time()
        if len(self._consumed) > _CONSUMED_WINDOW:
            cutoff = time.time() - 3600
            self._consumed = {k: v for k, v in self._consumed.items() if v > cutoff}
        async with self._lock:
            task = self.pending.pop(request_id, None)
        if task is None:
            log.warning("无对应任务的 result（request_id=%s）", request_id)
            return
        if not task.future.done():
            task.future.set_result(payload)
        # op 字段（04 §4.2）：前端据此区分「写任务完成」与「读任务完成」——
        # 只对写集失效 drift/gate。无 op 时（旧前端忽略未知字段）不炸；
        # 曾因前端把一切 task_done 当数据变更信号，读任务重取自身又产生
        # task_done，形成 gate-summary/status 请求自激风暴。
        await self.broadcast_ui({
            "type": "task_done", "task_id": task.task_id, "op": task.op,
            "summary": {"ok": bool(payload.get("ok"))},
        }, user_id=task.user_id)

    # ---------------------------------------------------------------- 任务下发

    async def submit_task(self, user_id: str, op: str, params: dict,
                          timeout: float = 60.0) -> dict:
        """向该用户的在线 daemon 下发任务并等待结果。

        request_id 云端生成、全局唯一（07 N12）；无 request_id 的任务 daemon 会拒绝。
        """
        from skillos.core.constants import WS_TASK_OPS

        if op not in WS_TASK_OPS:
            raise ValueError(f"非白名单操作: {op}（任务表即白名单，OUT-14）")
        agents = [a for a in self.agents.values() if a.user_id == user_id]
        if not agents:
            raise ConnectionError("DAEMON_OFFLINE")
        agent = agents[0]
        request_id = f"req_{secrets.token_hex(12)}"
        task_id = f"t_{secrets.token_hex(6)}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        async with self._lock:
            self.pending[request_id] = PendingTask(
                request_id=request_id, task_id=task_id, user_id=user_id,
                op=op, params=params, future=future,
            )
        try:
            await agent.ws.send_json({
                "type": "task", "task_id": task_id, "request_id": request_id,
                "op": op, "params": params,
            })
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            async with self._lock:
                self.pending.pop(request_id, None)
            return {"ok": False, "error": {"code": "TIMEOUT", "message": "daemon 未在时限内响应"}}

    # ---------------------------------------------------------------- UI 通道

    async def register_ui(self, ws: WebSocket, user_id: str) -> None:
        """注册 UI 连接 + **立即补发 daemon 状态快照**（FR-2.1.5）。

        没有快照的时序 bug：daemon 先连上（daemon_status 广播时 UI 还没连），
        再打开页面 → 徽标永远停在初始 offline，直到 daemon 下次断连/重连。
        """
        self.ui_conns[ws] = user_id
        await ws.send_json({"type": "daemon_status",
                            "connected": self.agent_online(user_id)})

    def unregister_ui(self, ws: WebSocket) -> None:
        self.ui_conns.pop(ws, None)

    async def broadcast_ui(self, msg: dict, user_id: str | None = None) -> None:
        """UI 广播。user_id 非空时只发给该用户的连接（用户维度状态不跨户）。
        单连接失败不影响其余（推送尽力而为，前端靠 Query 失效兜底）。"""
        for ws, uid in list(self.ui_conns.items()):
            if user_id is not None and uid != user_id:
                continue
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001
                self.ui_conns.pop(ws, None)
