"""全局常量集中地（04 §3 工程红线 11 / T2）。

阈值等魔法数字不得散落各处，统一从本模块引用。
"""

# T2：单文件超此阈值 => in_git=False、不参与漂移哈希、不分发（含此类资产的 Skill 禁止显示 SYNCED）
ASSET_MAX_BYTES: int = 5 * 1024 * 1024  # 默认 5MB，可配

# FR-1.0.17：三入口（CLI / daemon / MCP）共享的全局写锁
LOCK_FILE_NAME: str = ".lock"
LOCK_TIMEOUT_S: int = 30  # 超时报错退出，不静默跳过

# FR-1.2.3 / FR-3.15：配对码与限流
PAIRING_CODE_TTL_S: int = 600          # 6 位一次性码，10 分钟有效
PAIRING_MAX_FAILURES: int = 5          # 连续失败 5 次
PAIRING_LOCK_S: int = 60               # 锁定 60s

# daemon WS 重连：指数退避 1s,2s,4s,8s,…,60s 上限
WS_BACKOFF_MAX_S: int = 60

# WS 任务表（4.2）：任务表即白名单（OUT-14），新增能力必须加具名 op
WS_TASK_OPS: frozenset[str] = frozenset(
    {"scan", "status", "push", "pull", "file_read", "file_write", "version_log", "diff", "rollback"}
)

# 大文件策略：描述未纳管资产的徽标文案（FR-1.0.20）
UNTRACKED_ASSET_LABEL: str = "含未纳管资产"
