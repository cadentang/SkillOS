"""上游检查与升级（FR-2.2.10）。上游事实唯一权威在 upstream_sources 表。"""

from __future__ import annotations


class UpstreamService:
    def __init__(self) -> None:
        raise NotImplementedError("P2 实现")
