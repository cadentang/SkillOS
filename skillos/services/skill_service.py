"""Skill CRUD、标签、依赖、搜索（FR-2.1.6 / FR-2.2.1）。事务边界在本层。"""

from __future__ import annotations


class SkillService:
    def __init__(self) -> None:
        # P1：注入 IndexStore 与 GitRepo（root 显式传入，T1）
        raise NotImplementedError("P1 实现")
