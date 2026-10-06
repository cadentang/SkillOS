"""漂移计算、push、pull、冲突解决（FR-2.1.4 / FR-2.2.6 / FR-3.4）。

push 主流程（04 §5.2）：
  校验 → 读 canonical 快照 → render_layout → drift 判定
  （CONFLICT 依 on_conflict；adapter_outdated 先应用新模板；SYNCED 跳过幂等）
  → 备份 → 原子交换 → 更新 runtime_bindings（baseline_source→distributed）→ sync_records → WS 事件

冲突解决（04 §5.3）：整文件三选一（保留本地 / 保留仓库 / 暂不处理），
不做块内合并（OUT-11）；base 由 installed_commit + adapter_version 现算。
force 只在 Web 人工路径可达（FR-1.3.5）。

所有写操作必须经 services.locking.write_lock（FR-1.0.17）。
"""

from __future__ import annotations


class SyncService:
    def __init__(self) -> None:
        raise NotImplementedError("P1 实现")
