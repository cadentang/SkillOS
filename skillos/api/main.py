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
import os
import secrets
import time

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from skillos.api.routes import (
    auth,
    checks,
    conflicts,
    daemon,
    files,
    logs,
    protocol,
    reports,
    rules,
    runtimes,
    settings,
    skills,
    sync,
    teams,
    upstream,
    usage,
    versions,
)
from skillos.api.routes import sandbox as sandbox_routes
from skillos.api.security import hash_token, new_agent_token
from skillos.api.web import mount_web
from skillos.api.ws_hub import AgentConn, WsHub
from skillos.core.constants import WS_EVENTS
from skillos.core.logging import (
    bind_op_id,
    get_logger,
    get_op_id,
    new_op_id,
    reset_op_context,
    setup_logging,
)
from skillos.services.engine import resolve_home
from skillos.store.cloud_repo import CloudRepo
from skillos.store.db import init_schema, make_engine, make_session_factory

log = get_logger("skillos.api")


def _harden_secret_file(path) -> None:
    """POSIX 下收紧权限到 0600；Windows 家目录本身按用户 ACL 隔离。"""
    try:
        os.chmod(path, 0o600)
    except OSError:  # Windows 上 chmod 仅切换只读位，失败无安全影响
        pass


def _dev_jwt_secret() -> tuple[str, bool]:
    """开发态 JWT secret（env 未设时）：`<home>/.skillos/jwt_secret` 持久化。

    首启生成并写入，此后每次启动复用 —— 进程重启后已签发 token 不再失效。
    home 走 resolve_home()（T1）：沙盒下 SKILLOS_ROOT 指向容器内目录，
    secret 与该环境的库、日志落在同一边界内。
    写入失败（只读 FS 等）→ 退回进程随机值：只回到"重启失效"，
    不为密钥持久化阻塞服务启动。
    """
    path = resolve_home() / ".skillos" / "jwt_secret"
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing, True
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(32)
        path.write_text(secret + "\n", encoding="utf-8")
        _harden_secret_file(path)
        return secret, True
    except OSError:
        return secrets.token_urlsafe(32), False


def create_app(db_url: str | None = None, jwt_secret: str | None = None,
               sandbox: bool | None = None) -> FastAPI:
    """构建应用。缺省从环境取 DB URL 与 JWT secret。

    JWT secret 优先级：入参 > SKILLOS_JWT_SECRET > ~/.skillos/jwt_secret
    （首启生成持久化，重启复用）；持久化失败才退回进程随机值。
    sandbox：沙盒模式标志（SKILLOS_SANDBOX=1），None = 按环境判定。
    沙盒模式下 health 暴露该标志（前端演示横幅）并挂载 reset 路由（FR-3.10）。
    """
    # 容器部署日志：JSON 到 stdout（Docker 收集），不落 ~/.skillos/logs
    # （04 §6.2.4 Sink 表：云端 api 行）
    setup_logging("api")

    db_url = db_url or os.environ.get(
        "SKILLOS_DB_URL", "sqlite:///./skillos-cloud.db"
    )
    jwt_secret = jwt_secret or os.environ.get("SKILLOS_JWT_SECRET")
    if jwt_secret is None:
        # 开发态兜底：首启生成并持久化（见 _dev_jwt_secret），此后复用。
        # 只有持久化也失败时才退回进程随机值（重启失效）。
        jwt_secret, persisted = _dev_jwt_secret()
        if persisted:
            log.info(
                "jwt_secret_dev_ready",
                detail="env 未设 SKILLOS_JWT_SECRET，使用 ~/.skillos/jwt_secret 持久值",
            )
        else:
            log.warning(
                "jwt_secret_missing",
                detail="secret 持久化失败，使用进程随机值，重启后所有 token 失效",
            )
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

    # op_id middleware（FR-1.1.11 / T23）：每个 HTTP 请求绑定 op_id
    # 用于 cross-request 排错；响应头 `X-Request-Id` 回写便于客户端捕获
    @app.middleware("http")
    async def _op_id_middleware(request: Request, call_next):
        # 读 header 缺失则生成；保证前后端对齐
        op_id = request.headers.get("X-Request-Id") or new_op_id()
        bind_op_id(component="http", op_id=op_id,
                   method=request.method, path=request.url.path)
        t0 = time.monotonic()
        try:
            response = await call_next(request)
            duration_ms = int((time.monotonic() - t0) * 1000)
            log.info(
                "http_request_completed",
                http_method=request.method, http_path=request.url.path,
                http_status=response.status_code,
                duration_ms=duration_ms,
            )
            response.headers["X-Request-Id"] = op_id
            return response
        except Exception:
            duration_ms = int((time.monotonic() - t0) * 1000)
            log.exception(
                "http_request_failed",
                http_method=request.method, http_path=request.url.path,
                duration_ms=duration_ms,
            )
            raise
        finally:
            reset_op_context()

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        """统一 ERROR 记录 + 错误信封（04 §6.1）。"""
        op_id = get_op_id() or new_op_id()
        log.exception(
            "unhandled_exception",
            http_method=request.method, http_path=request.url.path,
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content={
                "ok": False, "data": None,
                "error": {"code": "INTERNAL", "message": "服务内部错误"},
            },
            headers={"X-Request-Id": op_id},
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
        daemon.router,  # FR-2.4.10 Settings "守护状态" 区块
        protocol.router,  # FR-2.4.10 Settings "协议版本" 区块
        teams.router,
        logs.router,  # FR-1.1.12：daemon 日志查看代理
        settings.router,  # V2-T1 设备侧开关（采集 / 上报 / L2 路径）
        usage.router,     # N-01.7/8 管理者视图 + N-11.7 调用记录
        reports.router,   # N-02 体检报告（本地表，daemon 生成）
        rules.router,     # N-03/N-08 规则面板 + E-04/N-07 相似对
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
        # daemon WS 连接级 op_id（与上层连接 / 任务绑定）
        bind_op_id(component="ws_agent")
        hello: dict = {}
        try:
            hello = await _recv_json(ws, timeout=15.0)
        except TimeoutError:
            log.warning(
                "ws_hello_timeout",
            )
            await ws.close(code=4000, reason="hello timeout")
            return
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            log.warning(
                "ws_hello_invalid",
            )
            await ws.close(code=4000, reason="expected hello")
            return
        # hello 到达后补绑 agent_id（下一轮 bind 合并上下文）
        bind_op_id(component="ws_agent", agent_id=str(hello.get("agent_id") or ""))

        agent_id = str(hello.get("agent_id") or "")
        token = str(hello.get("token") or "")
        pair_code = str(hello.get("pair_code") or "")
        platform_ = str(hello.get("platform") or "") or None

        # Session 生命周期必须与 WS 连接解耦：daemon 连接是小时级长连接，
        # Session 挂在连接上会在首次查询后永久占住一个池连接；认证失败
        # 路径（deny / 异常）不 commit 也不 close → 连接泄漏，QueuePool
        # 5+10 被 daemon 重连循环打满后，所有 hello 排队 30s 超时。
        session = app.state.session_factory()
        repo = CloudRepo(session)
        conn: AgentConn | None = None
        try:
            if token:
                tok = repo.find_token_by_hash(hash_token(token))
                if tok is None:
                    log.warning(
                        "auth_failed",
                        reason="token 无效或已撤销",
                        agent_id=agent_id,
                    )
                    await _deny(ws, "AUTH_FAILED", "token 无效或已撤销")
                    return
                repo.touch_token(tok.id)
                conn = AgentConn(ws=ws, agent_id=tok.agent_id or agent_id,
                                 user_id=tok.user_id, token_id=tok.id)
                # 回带 user_id：daemon 上报漂移事件需要归属（云端不猜）
                await ws.send_json({"type": "welcome", "user_id": tok.user_id})
                log.info(
                    "ws_agent_connected",
                    agent_id=conn.agent_id,
                    user_id=conn.user_id,
                    auth_mode="token",
                )
            elif pair_code:
                ip = ws.client.host if ws.client else "unknown"
                ip_scope = f"ip:{ip}"
                log.info(
                    "pair_started",
                    ip=ip,
                    agent_id=agent_id,
                )
                # ① ip 维度锁定检查先行（枚举者未必登录，04 §5.1 / FR-3.15）
                allowed, retry = repo.check_scope(ip_scope)
                if not allowed:
                    log.warning(
                        "rate_limited",
                        scope="ip", retry_s=retry,
                        agent_id=agent_id,
                    )
                    await _deny(ws, "RATE_LIMITED", f"配对尝试过多，{retry}s 后重试")
                    return
                # ② peek 码归属（不消费）→ user 维度锁定检查
                pc = repo.peek_pairing_code(pair_code)
                if pc is not None:
                    ok_u, retry_u = repo.check_scope(f"user:{pc.user_id}")
                    if not ok_u:
                        log.warning(
                            "rate_limited",
                            scope="user", retry_s=retry_u,
                            agent_id=agent_id,
                        )
                        await _deny(ws, "RATE_LIMITED", f"配对尝试过多，{retry_u}s 后重试")
                        return
                # ③ 校验消费码；失败双维度计数（码存在但已用/过期时归属可查）
                consumed = repo.consume_pairing_code(pair_code)
                if consumed is None:
                    fail_user = pc.user_id if pc is not None else None
                    if fail_user:
                        repo.record_scope_failure(f"user:{fail_user}")
                    repo.record_scope_failure(ip_scope)
                    log.warning(
                        "pair_failed",
                        reason="码无效或已使用",
                        agent_id=agent_id,
                    )
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
                log.info(
                    "pair_succeeded",
                    user_id=consumed.user_id,
                    agent_id=agent_id,
                )
            else:
                log.warning(
                    "auth_failed",
                    reason="缺少 token 或 pair_code",
                    agent_id=agent_id,
                )
                await _deny(ws, "AUTH_FAILED", "缺少 token 或 pair_code")
                return

            await app.state.hub.register_agent(conn)

            # 重连自愈（FR-1.0.34）：daemon 一上线立即对齐 custom Runtime。
            # 离线期间云端建的 custom（如 custom:trae）在此补推 →
            # 漂移矩阵列 / 分发目标即时可用，不依赖用户刷新页面。
            # fire-and-forget：对齐失败不影响 WS 循环，下次读时再试。
            async def _sync_custom_on_connect(uid: str = conn.user_id) -> None:
                try:
                    from skillos.api.routes.runtimes import _push_custom_sync
                    await _push_custom_sync(app, uid)
                except Exception:  # noqa: BLE001 —— 自愈尽力而为
                    pass
            asyncio.create_task(_sync_custom_on_connect())
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
                        await _handle_event(app, msg, conn)
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
            log.exception(
                "ws_agent_loop_error",
            )
            try:
                await ws.close(code=1011)
            except Exception:  # noqa: BLE001
                pass
        finally:
            # 覆盖全部退出路径（deny / 异常 / 断连）确定性归还池连接。
            # close 幂等；detach 后 tok 的已加载属性仍可读（expire_on_commit=False）。
            try:
                if conn is not None:
                    log.info(
                        "ws_agent_disconnected",
                        agent_id=conn.agent_id,
                        user_id=conn.user_id,
                    )
            except Exception:  # noqa: BLE001
                pass
            session.close()
            reset_op_context()

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
        await app.state.hub.register_ui(ws, user_id)
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
    # 服务端必须留痕：deny 静默会让"秒开秒关循环"无从排查（只看到连接关了）
    log.info("agent 握手被拒: %s - %s", code, message)
    await ws.send_json({"type": "error", "error": {"code": code, "message": message}})
    await ws.close(code=4003, reason=code)


async def _handle_event(app: FastAPI, msg: dict, conn: AgentConn) -> None:
    """daemon 上行事件（WS_EVENTS 白名单）：
    - `drift_changed` → upsert 漂移矩阵 + UI 广播（FR-1.2.6）
    - `usage_report` → 清洗合并用量聚合行 + ack + UI 广播（N-01.4/9）

    归属一律取**认证连接**（conn.user_id / conn.agent_id），不采信报文里的
    `user_id` —— 否则任何一台 daemon 都能往别人账号里写数据。
    每次事件新开短会话：认证会话已随注册关闭，长连接期间不能有
    Session 挂住池连接（见 ws_agent 内注释）。
    """
    event = msg.get("event")
    payload = msg.get("payload") or {}
    if event not in WS_EVENTS:
        await conn.ws.send_json({"type": "error", "error": {
            "code": "UNKNOWN_EVENT", "message": f"未登记的上行事件: {event}",
        }})
        return
    user_id = conn.user_id

    if event == "usage_report":
        await _ingest_usage_report(app, conn, payload)
        return

    from skillos.store.cloud_repo import drift_summary

    with app.state.session_factory() as session:
        repo = CloudRepo(session)
        repo.upsert_matrix(user_id, payload.get("matrix") or [])
        summary = drift_summary(repo.list_bindings(user_id))
    await app.state.hub.broadcast_ui({
        "type": "drift_changed", "summary": summary,
    }, user_id=user_id)


async def _ingest_usage_report(app: FastAPI, conn: AgentConn, payload: dict) -> None:
    """合并 daemon 主动上报的聚合行（N-01.4 每日上报 / N-01.9 重连补报）。

    幂等靠 `merge_usage_daily` 的**覆盖**语义：同一份 30 天全量重发不翻倍。
    `device_id` 用 `conn.device_id`（= token_id，每设备唯一）盖章 —— 设备自报值一律不用。
    ack 里回 `merged` / `skipped`，让设备侧日志能证明"报上去了"而不是"发出去了"。
    """
    from skillos.core.algorithm.usage import sanitize_report_rows

    try:
        rows, skipped = sanitize_report_rows(
            payload.get("rows"), device_id=conn.device_id,
        )
    except ValueError as exc:
        log.warning("usage_report_rejected", agent_id=conn.agent_id, reason=str(exc))
        await conn.ws.send_json({"type": "event_ack", "event": "usage_report",
                                 "ok": False,
                                 "error": {"code": "INVALID_INPUT", "message": str(exc)}})
        return

    with app.state.session_factory() as session:
        merged = CloudRepo(session).merge_usage_daily(rows)
    log.info("usage_report_merged", agent_id=conn.agent_id,
             user_id=conn.user_id, merged=merged, skipped=skipped)
    await conn.ws.send_json({"type": "event_ack", "event": "usage_report",
                             "ok": True, "merged": merged, "skipped": skipped})
    await app.state.hub.broadcast_ui({"type": "usage_changed", "merged": merged},
                                     user_id=conn.user_id)
