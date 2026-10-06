"""版本列表、diff、回滚（FR-2.2.5 / FR-2.2.7 / FR-2.2.8）。

回滚走 GitRepo.rollback：checkout + 新 commit，不重写历史。
"""

from __future__ import annotations


class VersionService:
    def __init__(self) -> None:
        raise NotImplementedError("P1 实现")
