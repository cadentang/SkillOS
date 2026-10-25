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
        "usage_aggregate",   # V2-T8：日聚合重算 + 事件清理（5.2）

        "usage_report",      # N-01.4：云端拉取本机聚合行（只读、只报聚合值）
        "file_history", "tag_list", "tag_create",
        # P3：CONFLICT 四选一（FR-2.2.6 / T22）。Web 人工路径专享；
        # MCP 工具签名故意不暴露 keep_both 选项（test_conflicts_p3 红线守护）。
        "resolve_conflict",
        # P3 CRUD（FR-2.1.7 新建 / 删除守卫 FR-1.0.26 ②）
        "skill_create", "skill_delete",
        # N-07.3 回收落选方（dry_run 先列 removals，逐条确认后才动）：
        # 软状态 —— canonical 不删，只回收 runtime 副本 + 打 deprecated 标记
        "skill_deprecate",
        # E-08.1 上游检查（单 skill / 全量）：具名 op，参数只认 slug
        "upstream_check",
        # P7 沙盒重置（FR-3.10）：engine.sandbox_reset 三层检查之一
        # （engine.is_sandbox / 本分支 / API 404），非沙盒部署不可达
        "sandbox_reset",
        # P5 日志查看（FR-1.1.12）：具名领域操作，不暴露任意文件读取
        # （路径固定为 ~/.skillos/logs/<mode>.log，参数白名单化）
        "logs",
        # P3 Settings 页（FR-2.4.10 / T26）：Runtime 自动发现 / 路径诊断 / 协议状态
        # 仅读；写操作（POST / PATCH / DELETE custom Runtime）走云端 cloud_repo，
        # daemon 不参与（不暴露 "force"/"git" 类通用操作，OUT-14）
        "runtimes_list", "runtime_diagnose", "protocol_status",
        "runtime_set_enabled", "runtime_sync_custom",
        # V2-M2（N-02 / N-01.7~8 / N-11.7 / V2-T1）：本地数据只经 daemon 取，
        # 云端不碰磁盘（红线 5）。全部具名、参数白名单化，无通用透传（OUT-14）。
        "settings_get", "settings_put",   # 设备侧开关（采集 / 上报 / L2 路径）
        "usage_summary",                  # 管理者视图：Top / 零调用 / 僵尸口径
        "usage_events",                   # 单 skill 调用记录 + session 调用链
        "report_list", "report_get",      # 体检报告列表 / 详情
        # V2-M3（N-03 / N-08 / E-04）：规则面板与相似对。这里唯一"写"的是重算相似缓存，
        # 不改 canonical、不删文件（N-07 的回收走 skill_delete 守卫）。
        "rules_list",                     # 规则面板：生效 / 无效 / 白名单 / ruleset_hash
        "similarity_list", "similarity_run",   # 相似对读取 / 立即比对（TQ-03）
    }
)

# WS 上行事件白名单（daemon → 云端）。与 WS_TASK_OPS 同一原则（OUT-14）：
# 云端只认登记过的事件，未登记的丢弃并回 UNKNOWN_EVENT —— 新增上行能力必须先改这里
WS_EVENTS: frozenset[str] = frozenset({"drift_changed", "usage_report"})

# N-01.5 / NFR-V2-03：上报行**唯一**允许出现的字段集。
# 设备侧构造与云端二次校验共用这一份 —— 两份白名单迟早漂移，
# 漂移的后果是「设备侧多塞的字段被自动带上云」，那正是隐私边界失效的方式。
USAGE_REPORT_FIELDS: frozenset[str] = frozenset({
    "device_id", "skill_id", "date", "via", "count",
    "tokens_in", "tokens_out", "duration_ms_p50", "duration_ms_p95", "by_op",
})
# 单次上行行数上限（30 天 × skill × via × 通道数，留足余量）。
# 超限**报错**而不是截断 —— 截断就是静默丢数据。
USAGE_REPORT_MAX_ROWS: int = 20000

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

# ---------------------------------------------------------------- 健康度（E-01）
# FR-1.0.24 / E-01.1：score = 100 − 10×error − 3×warn − 8×degraded_decay_dimensions，下限 0。
# E-01.2：**禁止做成 DB / 配置可调** —— 健康度是缓存可重算值，运行时可配会让历史分数
# 不可比（分不清是资产变了还是权重变了）。调权重 = 改代码 + 提交说明。
HEALTH_WEIGHTS: dict[str, int] = {
    "error": 10,
    "warn": 3,
    "degraded_decay": 8,
}
HEALTH_SCORE_MAX: int = 100
HEALTH_SCORE_FLOOR: int = 0

# ---------------------------------------------------------------- 折旧（E-02）
# E-02.2：无引用 90 天判陈旧；被引用 ≥1 次放宽到 180 天（被引用的资产不算僵尸）。
# decay 状态集只有 ok/degraded/unknown，无 warn 档。
STALENESS_DEGRADED_DAYS: int = 90
STALENESS_DEGRADED_DAYS_RELAXED: int = 180

# ---------------------------------------------------------------- 门禁阈值（E-03）
# E-03.3：原先散落在 checks.py 顶部，集中到此处。
DESC_MIN_CHARS: int = 30
DESC_MAX_CHARS: int = 500
DESC_TOKEN_HINT: int = 150
