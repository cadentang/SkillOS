"""FastAPI 应用（04 §2.6，FR-3.11）。

- 统一响应包装：{"ok": bool, "data": ..., "error": {...} | null}
- 错误码：UNAUTHORIZED / FORBIDDEN / NOT_FOUND / VALIDATION / CONFLICT /
  STALE / PATH_TRAVERSAL / DAEMON_OFFLINE / INTERNAL
- 云端不碰磁盘（红线 5）、不缓存文件内容（红线 6，FR-3.12）

create_app 工厂：测试注入独立 SQLite 与 JWT secret，互不串库。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from skillos.api.routes import (
    auth,
    checks,
    conflicts,
    files,
    runtimes,
    skills,
    sync,
    teams,
    upstream,
    versions,
)
from skillos.api.routes import sandbox as sandbox_routes
from skillos.api.security import hash_token, new_agent_token
from skillos.api.web import mount_web
from skillos.api.ws_hub import AgentConn, WsHub
from skillos.store.cloud_repo import CloudRepo
from skillos.store.db import init_schema, make_engine, make_session_factory

log = logging.getLogger("skillos.api")


def create_app(db_url: str | None = None, jwt_secret: str | None = None,
               sandbox: bool | None = None) -> FastAPI:
    """构建应用。缺省从环境取 DB URL 与 JWT secret；开发态自动生成（重启失效）。

    sandbox：沙盒模式标志（SKILLOS_SANDBOX=1），None = 按环境判定。
    沙盒模式下 health 暴露该标志（前端演示横幅）并挂载 reset 路由（FR-3.10）。
    """
    db_url = db_url or os.environ.get(
        "SKILLOS_DB_URL", "sqlite:///./skillos-cloud.db"
    )
    jwt_secret = jwt_secret or os.environ.get("SKILLOS_JWT_SECRET")
    if jwt_secret is None:
        jwt_secret = secrets.token_urlsafe(32)
        log.warning("SKILLOS_JWT_SECRET 未设置，使用进程随机值（重启后 token 全失效）")
    if sandbox is None:
        sandbox = os.environ.get("SKILLOS_SANDBOX") == "1"

    app = FastAPI(
        title="Skill OS Cloud",
        version="0.1.0",
        description="Skill OS 控制面 —— 元数据 / 协作 / 任务下发；数据面在本地 daemon",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    engine = make_engine(db_url)
    init_schema(engine)
    app.state.session_factory = make_session_factory(engine)
    app.state.jwt_secret = jwt_secret
    app.state.hub = WsHub()
    app.state.sandbox = sandbox

    routers = (
        auth.router,
        skills.router,
        files.router,
        versions.router,
        sync.router,
        checks.router,
        conflicts.router,
        upstream.router,
        runtimes.router,
        teams.router,
    )
    if sandbox:
        # 仅沙盒部署存在 reset 入口（04 §2.7.7）；云端不碰磁盘，恢复在 daemon 侧
        routers = (*routers, sandbox_routes.router)
    for router in routers:
        app.include_router(router, prefix="/api")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "data": {"status": "up", "sandbox": sandbox}, "error": None}

    @app.websocket("/ws/agent")
    async def ws_agent(ws: WebSocket) -> None:
        """daemon 通道：hello（token 或配对码）→ task/result/event/ping。

        配对流程（04 §5.1）：限流检查 → 校验码 → 签发 token（只存哈希）→
        发 paired 消息回 daemon → 注册连接。
        """
        await ws.accept()
        hello: dict = {}
        try:
            hello = await _recv_json(ws, timeout=15.0)
        except TimeoutError:
            await ws.close(code=4000, reason="hello timeout")
            return
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            await ws.close(code=4000, reason="expected hello")
            return

        agent_id = str(hello.get("agent_id") or "")
        token = str(hello.get("token") or "")
        pair_code = str(hello.get("pair_code") or "")
        platform_ = str(hello.get("platform") or "") or None

        repo = CloudRepo(app.state.session_factory())
        conn: AgentConn | None = None
        try:
            if token:
                tok = repo.find_token_by_hash(hash_token(token))
                if tok is None:
                    await _deny(ws, "AUTH_FAILED", "token 无效或已撤销")
                    return
                repo.touch_token(tok.id)
                conn = AgentConn(ws=ws, agent_id=tok.agent_id or agent_id,
                                 user_id=tok.user_id, token_id=tok.id)
                # 回带 user_id：daemon 上报漂移事件需要归属（云端不猜）
                await ws.send_json({"type": "welcome", "user_id": tok.user_id})
            elif pair_code:
                ip = ws.client.host if ws.client else "unknown"
                ip_scope = f"ip:{ip}"
                # ① ip 维度锁定检查先行（枚举者未必登录，04 §5.1 / FR-3.15）
                allowed, retry = repo.check_scope(ip_scope)
                if not allowed:
                    await _deny(ws, "RATE_LIMITED", f"配对尝试过多，{retry}s 后重试")
                    return
                # ② peek 码归属（不消费）→ user 维度锁定检查
                pc = repo.peek_pairing_code(pair_code)
                if pc is not None:
                    ok_u, retry_u = repo.check_scope(f"user:{pc.user_id}")
                    if not ok_u:
                        await _deny(ws, "RATE_LIMITED", f"配对尝试过多，{retry_u}s 后重试")
                        return
                # ③ 校验消费码；失败双维度计数（码存在但已用/过期时归属可查）
                consumed = repo.consume_pairing_code(pair_code)
                if consumed is None:
                    fail_user = pc.user_id if pc is not None else None
                    if fail_user:
                        repo.record_scope_failure(f"user:{fail_user}")
                    repo.record_scope_failure(ip_scope)
                    await _deny(ws, "PAIRING_FAILED", "配对码无效或已使用")
                    return
                token_id, plain, token_hash = new_agent_token()
                repo.issue_agent_token(
                    consumed.user_id, agent_id, token_id, token_hash,
                    platform=platform_, name=hello.get("name"),
                )
                repo.record_pairing_success(consumed.user_id, ip)
                await ws.send_json({"type": "paired", "token": plain,
                                    "user_id": consumed.user_id})
                conn = AgentConn(ws=ws, agent_id=agent_id,
                                 user_id=consumed.user_id, token_id=token_id)
            else:
                await _deny(ws, "AUTH_FAILED", "缺少 token 或 pair_code")
                return

            await app.state.hub.register_agent(conn)
            try:
                while True:
                    msg = await _recv_json(ws, timeout=None)
                    mtype = msg.get("type")
                    if mtype == "ping":
                        await ws.send_json({"type": "pong"})
                    elif mtype == "result":
                        req_id = str(msg.get("request_id") or "")
                        if not req_id:
                            await ws.send_json({
                                "type": "error", "error": {
                                    "code": "INVALID_INPUT",
                                    "message": "result 缺少 request_id（07 N12）",
                                },
                            })
                            continue
                        await app.state.hub.handle_result(req_id, msg)
                    elif mtype == "event":
                        await _handle_event(app, repo, msg)
                    else:
                        await ws.send_json({
                            "type": "error",
                            "error": {"code": "INVALID_INPUT", "message": f"未知消息类型 {mtype}"},
                        })
            finally:
                # 覆盖断连 / 异常全部路径；unregister 幂等
                if conn is not None:
                    await app.state.hub.unregister_agent(conn)
        except WebSocketDisconnect:
            pass  # finally 已收尾
        except Exception:  # noqa: BLE001 —— WS 循环内任何异常都不能带崩整个服务
            log.exception("agent ws loop error")
            try:
                await ws.close(code=1011)
            except Exception:  # noqa: BLE001
                pass

    @app.websocket("/ws/ui")
    async def ws_ui(ws: WebSocket) -> None:
        """UI 订阅通道（FR-2.4.4）：token 经 query 参数。"""
        from skillos.api.security import verify_jwt

        token = ws.query_params.get("token")
        user_id = verify_jwt(token, app.state.jwt_secret) if token else None
        if user_id is None:
            await ws.close(code=4001, reason="unauthorized")
            return
        await ws.accept()
        await app.state.hub.register_ui(ws)
        try:
            while True:
                # UI → 云端暂无上行消息；保活靠心跳帧（客户端 ping）
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            app.state.hub.unregister_ui(ws)

    # 静态托管必须放在**所有路由之后**（04 §1.4 容器 A；开发态 vite 5173 不挂载）。
    # Mount("/") 对空路径匹配所有 http/websocket scope，排前面会吞掉 /ws/*
    # —— 顺序即语义，见 skillos/api/web.py 与 tests/test_web_hosting.py。
    mount_web(app)

    return app


async def _recv_json(ws: WebSocket, timeout: float | None) -> dict:
    if timeout is not None:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=timeout)
    else:
        raw = await ws.receive_text()
    v = json.loads(raw)
    if not isinstance(v, dict):
        raise ValueError("expected object")
    return v


async def _deny(ws: WebSocket, code: str, message: str) -> None:
    await ws.send_json({"type": "error", "error": {"code": code, "message": message}})
    await ws.close(code=4003, reason=code)


async def _handle_event(app: FastAPI, repo: CloudRepo, msg: dict) -> None:
    """daemon 事件：drift_changed → upsert 漂移矩阵 + UI 广播（FR-1.2.6）。"""
    event = msg.get("event")
    payload = msg.get("payload") or {}
    if event != "drift_changed":
        return
    user_id = msg.get("user_id") or ""
    if not user_id:
        return
    repo.upsert_matrix(user_id, payload.get("matrix") or [])
    from skillos.store.cloud_repo import drift_summary

    summary = drift_summary(repo.list_bindings(user_id))
    await app.state.hub.broadcast_ui({
        "type": "drift_changed", "summary": summary,
    })
