"""版本 / diff / 回滚（FR-3.2）。实现待 P3。"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/skills", tags=["versions"])
