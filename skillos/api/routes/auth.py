"""认证 / 配对码 / 设备管理（FR-3.1 / FR-1.2.3 / FR-1.2.8 / FR-3.14 / FR-3.15）。

配对限流：user 与 ip 双维度，连续 5 次失败锁 60s（防 6 位码暴力枚举）。
token 撤销不物理删除，保留审计痕迹（FR-1.2.8）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from skillos.api.deps import get_current_user_id, get_repo
from skillos.api.security import hash_password, issue_jwt, verify_password
from skillos.store.cloud_repo import CloudRepo

router = APIRouter(tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    name: str | None = None
    password: str = Field(min_length=8)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


def _client_ip(request: Request) -> str:
    # 单容器部署：直连地址即可；有代理时取 X-Forwarded-For 首段
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/auth/register")
def register(body: RegisterIn, repo: CloudRepo = Depends(get_repo)) -> dict:
    if repo.get_user_by_email(body.email):
        raise HTTPException(status_code=409, detail="EMAIL_TAKEN")
    user = repo.create_user(body.email, body.name, hash_password(body.password))
    return {"ok": True, "data": {"id": user.id, "email": user.email}, "error": None}


@router.post("/auth/login")
def login(body: LoginIn, request: Request,
          repo: CloudRepo = Depends(get_repo)) -> dict:
    user = repo.get_user_by_email(body.email)
    if user is None or not verify_password(body.password, user.password_hash or ""):
        # 统一错误（不区分「用户不存在」与「密码错」）
        raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS")
    token = issue_jwt(user.id, request.app.state.jwt_secret)
    return {"ok": True, "data": {"token": token, "user": {"id": user.id,
                                                          "email": user.email}}, "error": None}


@router.get("/auth/me")
def me(user_id: str = Depends(get_current_user_id),
       repo: CloudRepo = Depends(get_repo)) -> dict:
    user = repo.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    return {"ok": True, "data": {"id": user.id, "email": user.email,
                                 "name": user.name}, "error": None}


@router.post("/pairing/code")
def issue_pairing_code(request: Request,
                       user_id: str = Depends(get_current_user_id),
                       repo: CloudRepo = Depends(get_repo)) -> dict:
    """UI 登录后签发 6 位一次性配对码（10 分钟有效）。限流在 daemon 握手校验侧。"""
    code = repo.issue_pairing_code(user_id)
    return {"ok": True, "data": {"code": code, "ttl_s": 600}, "error": None}


@router.get("/devices")
def list_devices(user_id: str = Depends(get_current_user_id),
                 repo: CloudRepo = Depends(get_repo)) -> dict:
    devices = repo.list_devices(user_id)
    return {"ok": True, "data": [
        {
            "id": d.id, "agent_id": d.agent_id, "name": d.name,
            "platform": d.platform,
            "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
            "revoked_at": d.revoked_at.isoformat() if d.revoked_at else None,
        } for d in devices
    ], "error": None}


@router.delete("/devices/{token_id}")
async def revoke_device(token_id: str, request: Request,
                        user_id: str = Depends(get_current_user_id),
                        repo: CloudRepo = Depends(get_repo)) -> dict:
    """撤销 token（FR-1.2.8）：置 revoked_at + 立即断连该 daemon。不物理删除。"""
    tok = repo.revoke_token(user_id, token_id)
    if tok is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    conn = request.app.state.hub.agents.get(tok.agent_id)
    if conn is not None:
        await conn.ws.close(code=4003, reason="token revoked")
    return {"ok": True, "data": {"id": tok.id, "revoked": True}, "error": None}
