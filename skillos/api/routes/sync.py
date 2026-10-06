"""漂移查询、push/pull 下发、冲突提交（FR-3.3 / FR-3.4）。

push 的 on_conflict=force 只在 Web 人工路径可达（FR-1.3.5）。
云端只下发任务，不执行磁盘操作（红线 5）。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/sync", tags=["sync"])
