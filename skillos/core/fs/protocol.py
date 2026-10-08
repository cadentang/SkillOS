"""仓库协议版本协商（04 §2.1.3 / FR-1.0.29 / T18 / 06 A4）。

adapter_version（FR-1.0.18）只解决单机内的模板漂移；跨客户端（多台机器、
不同 skillos 版本）的协议差异靠两个载体协商：

| 载体           | 内容                    | 规则                                  |
|----------------|-------------------------|---------------------------------------|
| commit trailer | `Skillos-Protocol: N`   | 每个应用层 commit 都带                 |
| protocol.json  | `{"protocol": N}` sticky | 只升不降；损坏则重写                   |

协商规则：
  树里有 protocol.json 但 commit 无对应 trailer  → 旧客户端写入
    → 拒绝混用（PROTOCOL_MISMATCH），提示升级而非静默继续
  commit 的 trailer 版本 > 本机支持版本           → 只读模式，禁止写入

单机 demo 不触发，但数据结构必须现在定 —— 否则 V2 加第二个客户端时要迁移历史。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from skillos.core.fs.meta import META_DIR

TRAILER_KEY = "Skillos-Protocol"
CURRENT_PROTOCOL = 1

_STICKY_FILE = "protocol.json"


class ProtocolMismatch(Exception):
    """协议不匹配（旧客户端写入 / 试图降级 sticky）。"""

    code = "PROTOCOL_MISMATCH"


@dataclass(frozen=True)
class ProtocolCheck:
    """写入前的协商结论。ok=False 或 read_only=True 时禁止写入。"""

    ok: bool
    read_only: bool
    reason: str | None = None


# ---------------------------------------------------------------- commit trailer


def append_trailer(message: str, version: int = CURRENT_PROTOCOL) -> str:
    """给 commit message 追加协议 trailer（幂等：已有则原样返回）。"""
    if parse_trailer(message) is not None:
        return message
    return f"{message}\n\n{TRAILER_KEY}: {version}"


def parse_trailer(message: str) -> int | None:
    """从 commit message 提取协议版本；缺失 / 非法返回 None（不猜）。"""
    for line in reversed(message.splitlines()):
        stripped = line.strip()
        if stripped.startswith(f"{TRAILER_KEY}:"):
            value = stripped.partition(":")[2].strip()
            try:
                return int(value)
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------- sticky 文件


def _sticky_path(repo_root: Path) -> Path:
    return repo_root / META_DIR / _STICKY_FILE


def read_sticky(repo_root: Path) -> int | None:
    """读 sticky 协议版本。缺失 / 损坏返回 None（不猜，调用方可重写）。"""
    path = _sticky_path(repo_root)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    value = raw.get("protocol")
    return value if isinstance(value, int) else None


def write_sticky(repo_root: Path, version: int) -> None:
    """写 sticky（原子）。只升不降：现值更高时抛 ProtocolMismatch。"""
    current = read_sticky(repo_root)
    if current is not None and version < current:
        raise ProtocolMismatch(
            f"sticky protocol must never downgrade: current={current}, tried={version}"
        )
    path = _sticky_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"protocol": version}) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 协商


def check(
    head_message: str | None,
    sticky_version: int | None,
    supported: int = CURRENT_PROTOCOL,
) -> ProtocolCheck:
    """写入前的双向校验（services 层在锁内、写 git 之前调用）。

    - sticky 存在但 HEAD commit 无 trailer → 旧客户端写入，拒绝混用
    - trailer / sticky 版本 > 本机支持 → 只读模式
    """
    trailer_version = parse_trailer(head_message) if head_message else None

    if sticky_version is not None and trailer_version is None:
        return ProtocolCheck(
            ok=False, read_only=True, reason="PROTOCOL_MISMATCH"
        )

    effective = trailer_version if trailer_version is not None else sticky_version
    if effective is not None and effective > supported:
        return ProtocolCheck(
            ok=False, read_only=True, reason="PROTOCOL_TOO_NEW"
        )
    return ProtocolCheck(ok=True, read_only=False)
