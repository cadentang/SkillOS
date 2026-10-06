"""文件树与内容（FR-3.2 / FR-3.12）。

- 文件树（path/size/hash/is_text）缓存于云端 —— Dashboard 漂移矩阵数据源
- 文件内容不进云端：GET file 一律下发 file_read 任务经 daemon 实时取回
- PUT file 强制 base_hash 乐观锁，不匹配 409 STALE（NFR-3.1）
- path 参数必须先过 core.fs.safe_path（PATH_TRAVERSAL 由其统一抛出）
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/skills", tags=["files"])
