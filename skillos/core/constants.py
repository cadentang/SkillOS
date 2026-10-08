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
    {
        "scan", "status", "push", "pull", "file_read", "file_write", "file_tree",
        "version_log", "diff", "rollback", "gate_summary", "check", "decay",
        "file_history", "tag_list", "tag_create",
        # P3：CONFLICT 四选一（FR-2.2.6 / T22）。Web 人工路径专享；
        # MCP 工具签名故意不暴露 keep_both 选项（test_conflicts_p3 红线守护）。
        "resolve_conflict",
        # P3 CRUD（FR-2.1.7 新建 / 删除守卫 FR-1.0.26 ②）
        "skill_create", "skill_delete",
        # P7 沙盒重置（FR-3.10）：engine.sandbox_reset 三层检查之一
        # （engine.is_sandbox / 本分支 / API 404），非沙盒部署不可达
        "sandbox_reset",
    }
)

# 大文件策略：描述未纳管资产的徽标文案（FR-1.0.20）
UNTRACKED_ASSET_LABEL: str = "含未纳管资产"

# FR-1.0.6 / FR-2.1.9：漂移七态严重度（大者为更严重），按 Skill 聚合取 max
DRIFT_SEVERITY: dict[str, int] = {
    "SYNCED": 0,
    "UNMANAGED": 1,
    "ORPHANED": 2,
    "MISSING": 3,
    "OUTDATED": 4,
    "MODIFIED": 5,
    "CONFLICT": 6,
}
