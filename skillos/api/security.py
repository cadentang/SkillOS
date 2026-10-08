"""认证原语（T4：云端 JWT；NFR-2：token 只存哈希）。

- 密码：标准库 scrypt（无编译依赖，跨平台；格式串自带参数，可升级）
- JWT：pyjwt HS256，短时效；secret 由部署注入，开发态自动生成并告警
- daemon token：secrets 生成，库中只存 sha256；明文只在签发响应中出现一次
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import jwt

JWT_ALGORITHM = "HS256"
JWT_TTL = timedelta(hours=12)
TOKEN_TTL_DAYS = 365


def hash_password(password: str) -> str:
    """scrypt 哈希。格式：scrypt$N$r$p$salt_hex$hash_hex（参数随串走，可升级）。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32
    )
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_hex, hash_hex = stored.split("$")
        digest = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def new_agent_token() -> tuple[str, str, str]:
    """生成 daemon 长期 token。返回 (token_id, 明文 token, token_hash)。

    明文只在签发时出现一次；库中只存 hash（NFR-2）。
    """
    token = secrets.token_urlsafe(32)
    return f"tok_{secrets.token_hex(6)}", token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_jwt(user_id: str, secret: str, ttl: timedelta = JWT_TTL) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + ttl},
        secret,
        algorithm=JWT_ALGORITHM,
    )


def verify_jwt(token: str, secret: str) -> str | None:
    """校验 JWT，返回 user_id；无效/过期返回 None（不抛，调用方统一 401）。"""
    try:
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None
