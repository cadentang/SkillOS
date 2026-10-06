"""FastAPI 应用（04 §2.6，FR-3.11）。

- 统一响应包装：{"ok": bool, "data": ..., "error": {...} | null}
- 错误码：UNAUTHORIZED / FORBIDDEN / NOT_FOUND / VALIDATION / CONFLICT /
  STALE / PATH_TRAVERSAL / DAEMON_OFFLINE / INTERNAL
- 云端不碰磁盘（红线 5）、不缓存文件内容（红线 6，FR-3.12）
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from skillos.api.routes import checks, files, runtimes, skills, sync, teams, upstream, versions

app = FastAPI(
    title="Skill OS Cloud",
    version="0.1.0",
    description="Skill OS 控制面 —— 元数据 / 协作 / 任务下发；数据面在本地 daemon",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (
    skills.router,
    files.router,
    versions.router,
    sync.router,
    checks.router,
    upstream.router,
    runtimes.router,
    teams.router,
):
    app.include_router(router, prefix="/api")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "data": {"status": "up"}, "error": None}
