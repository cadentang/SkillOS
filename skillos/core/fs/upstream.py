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


def check_upstream(source: UpstreamSource) -> UpstreamStatus:
    """检查上游是否有更新。实现待 P2。"""
    raise NotImplementedError("P2 实现：git ls-remote / marketplace 版本 / url ETag")
