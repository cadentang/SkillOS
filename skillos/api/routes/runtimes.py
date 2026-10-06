"""Runtime 列表与状态（GET /api/runtimes）。实现待 P3。"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/runtimes", tags=["runtimes"])
