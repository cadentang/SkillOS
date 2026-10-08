"""依赖注入：DB 会话、当前用户（FR-3.1）。

app.state 上挂 engine/session_factory/jwt_secret/repo —— 由 create_app 工厂注入，
测试可用独立 SQLite 实例，互不串库。
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Header, HTTPException, Request, WebSocket
from sqlalchemy.orm import Session

from skillos.api.security import verify_jwt
from skillos.store.cloud_repo import CloudRepo


def get_db(request: Request) -> Iterator[Session]:
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_repo(session: Session = Depends(get_db)) -> CloudRepo:
    return CloudRepo(session)


def _extract_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[len("Bearer "):]


def get_current_user_id(request: Request,
                        authorization: str | None = Header(default=None)) -> str:
    """JWT 校验（HTTP 路径）。无效统一 401，不区分过期/签名错误（不泄露细节）。"""
    token = _extract_token(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    user_id = verify_jwt(token, request.app.state.jwt_secret)
    if user_id is None:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    return user_id


def get_current_user_id_ws(request: Request, ws: WebSocket) -> str | None:
    """WS 路径：token 经 query 参数传递（浏览器 WS 无法带 Authorization 头）。"""
    token = ws.query_params.get("token")
    if not token:
        return None
    return verify_jwt(token, request.app.state.jwt_secret)
