"""上游跟踪（04 §2.2.4，FR-1.0.13）。

git: ls-remote 取 ref commit；marketplace: 查版本号；url: HEAD 请求取 ETag。
本地改动标记 is_fork=True，升级时走三方合并而非覆盖。
上游信息唯一权威在 upstream_sources 表（04 §3.2），skills 表不重复存放。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class UpstreamStatusKind(StrEnum):
    CURRENT = "current"
    OUTDATED = "outdated"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class UpstreamSource:
    skill_id: str
    type: str  # git / marketplace / url / local
    url: str | None
    ref: str | None
    base_version: str | None
    upstream_hash: str | None


@dataclass(frozen=True)
class UpstreamStatus:
    status: UpstreamStatusKind
    upstream_hash: str | None = None
    detail: str | None = None


def decide_status(source: UpstreamSource, remote_hash: str | None) -> UpstreamStatus:
    """按 E-08.2 / E-08.3 判定上游状态（纯函数：IO 结果由调用方取证后传入）。

    - `type=local` 恒 `current`：没有上游这件事本身就是"没落后"（E-08.2）；
    - 远端 hash 与记录一致 → `current`；不一致 → `outdated`（E-08.3 里
      对应 `upstream_divergence=degraded`）；
    - 记录里没有 baseline（从未成功比对过）→ `unknown`：不猜"大概是最新的"；
    - marketplace / url 两类尚无可靠的远端取证方式 → `unknown`，
      宁可显示未知，也不假装已核对（NFR-10.4）。

    网络失败不在这里表达：那是调用方的失败计数（E-08.4），本函数只处理"取到了"。
    """
    kind = (source.type or "").strip().lower()
    if kind == "local":
        return UpstreamStatus(UpstreamStatusKind.CURRENT, source.upstream_hash,
                              detail="local：无上游")
    if kind != "git":
        return UpstreamStatus(UpstreamStatusKind.UNKNOWN, source.upstream_hash,
                              detail=f"{kind or '未声明类型'}：暂无可靠取证方式")
    if remote_hash is None:
        return UpstreamStatus(UpstreamStatusKind.UNKNOWN, source.upstream_hash,
                              detail="远端 ref 不存在")
    if not source.upstream_hash:
        return UpstreamStatus(UpstreamStatusKind.UNKNOWN, remote_hash,
                              detail="缺少 baseline：首次比对只记录不判定")
    if remote_hash == source.upstream_hash:
        return UpstreamStatus(UpstreamStatusKind.CURRENT, remote_hash)
    return UpstreamStatus(UpstreamStatusKind.OUTDATED, remote_hash,
                          detail="上游 ref 已前进")
