"""SQLAlchemy 模型 —— 表结构严格按 04 §3.2，本地 SQLite 与云端 Postgres 同构。

派生字段不落库（T6 落地约束）：
- adapter_outdated = 当前 adapter 版本 != runtime_bindings.adapter_version，随时可算
- has_untracked_assets = 该 skill 存在 skill_files.in_git = false
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- 身份与归属


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # ULID
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    avatar: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(Text)  # bcrypt
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (
        CheckConstraint("role IN ('owner','member','viewer')", name="ck_member_role"),
    )

    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)


class PairingCode(Base):
    """daemon 配对码：6 位一次性，10 分钟有效。"""

    __tablename__ = "pairing_codes"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)  # 一次性


class PairingAttempt(Base):
    """配对限流（FR-1.2.3 / FR-3.15）：user 与 ip 双维度，5 次锁 60s。"""

    __tablename__ = "pairing_attempts"

    scope: Mapped[str] = mapped_column(Text, primary_key=True)  # "user:<id>" / "ip:<addr>"
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class AgentToken(Base):
    """daemon token：只存哈希（NFR-2）；revoked_at 非空即撤销，不物理删除（FR-1.2.8）。"""

    __tablename__ = "agent_tokens"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)  # 本机标识
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    platform: Mapped[str | None] = mapped_column(Text)  # windows / darwin / linux
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


# ---------------------------------------------------------------- Skill 主体


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = (
        CheckConstraint("visibility IN ('private','team')", name="ck_skill_visibility"),
        CheckConstraint(
            "source_kind IN ('local','git','marketplace','imported')", name="ck_skill_source"
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)  # kebab-case
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(Text, nullable=False, default="private")
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"))
    # 只描述来源类别；上游 url/ref/hash 唯一权威在 upstream_sources（FR-1.0.13）
    source_kind: Mapped[str] = mapped_column(Text, nullable=False, default="local")
    is_fork: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class SkillTag(Base):
    __tablename__ = "skill_tags"

    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), primary_key=True)
    tag: Mapped[str] = mapped_column(Text, primary_key=True)


class SkillDependency(Base):
    __tablename__ = "skill_dependencies"
    __table_args__ = (
        CheckConstraint("kind IN ('requires','recommends')", name="ck_dep_kind"),
    )

    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), primary_key=True)
    depends_on_skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)


# ---------------------------------------------------------------- 版本


class SkillVersion(Base):
    __tablename__ = "skill_versions"
    __table_args__ = (
        CheckConstraint("status IN ('draft','released','deprecated')", name="ck_version_status"),
        UniqueConstraint("skill_id", "version", name="uq_skill_version"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)  # semver
    git_commit: Mapped[str] = mapped_column(Text, nullable=False)  # 关联 Git commit
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    message: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class SkillFile(Base):
    """只存索引，内容在 Git。path 用 POSIX 分隔符（05 P2-3）。"""

    __tablename__ = "skill_files"
    __table_args__ = (
        UniqueConstraint("version_id", "path", name="uq_version_path"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("skill_versions.id"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    hash: Mapped[str] = mapped_column(Text, nullable=False)  # sha256
    size: Mapped[int | None] = mapped_column(Integer)
    is_text: Mapped[bool | None] = mapped_column(Boolean)
    # T2：False = 超阈值未纳管资产，不入 Git、不参与漂移哈希、不分发
    in_git: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


# ---------------------------------------------------------------- Runtime 与漂移


class Runtime(Base):
    __tablename__ = "runtimes"
    __table_args__ = (
        CheckConstraint("scope IN ('user','project')", name="ck_runtime_scope"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, nullable=False)  # claude/agents/workbuddy/codex
    name: Mapped[str | None] = mapped_column(Text)
    adapter_key: Mapped[str] = mapped_column(Text, nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False, default="user")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    # FR-1.0.32 / T25 / T26: discover_only = true 时该 Runtime 只在 Settings 诊断区出现，
    # 不进漂移矩阵列头 / 不参与 push 目标 / 不参与 binding 匹配 / 不参与 compare_normalized
    # 显式状态机：单一信号，"路径填了但忘了改标志"被默认行为吞掉是禁止的
    discover_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class RuntimeBinding(Base):
    """产品心脏表（04 §3.2）。

    base 不落盘（T6）：靠 installed_commit + adapter_version 现算；
    installed_version 仅展示用，不作任何基线。
    status 是判定结果缓存，任何时候必须能由磁盘+commit+adapter 版本重算（红线 10）。
    """

    __tablename__ = "runtime_bindings"
    __table_args__ = (
        CheckConstraint(
            "status IN ('synced','missing','outdated','modified','conflict',"
            "'unmanaged','orphaned')",
            name="ck_binding_status",
        ),
        CheckConstraint("baseline_source IN ('distributed','imported')", name="ck_baseline_src"),
        UniqueConstraint("skill_id", "runtime_id", name="uq_skill_runtime"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), nullable=False)
    runtime_id: Mapped[str] = mapped_column(ForeignKey("runtimes.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="unmanaged")
    # base 重建锚点（FR-1.0.18）
    installed_commit: Mapped[str | None] = mapped_column(Text)
    adapter_version: Mapped[str | None] = mapped_column(Text)
    baseline_source: Mapped[str] = mapped_column(Text, nullable=False, default="imported")
    # 磁盘基线：render(canonical@installed_commit, installed_adapter_version) 的哈希
    installed_hash: Mapped[str | None] = mapped_column(Text)
    installed_version: Mapped[str | None] = mapped_column(Text)  # 仅展示，不作基线
    installed_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)


class SyncRecord(Base):
    """所有写操作落库（NFR-9 审计）。"""

    __tablename__ = "sync_records"
    __table_args__ = (
        CheckConstraint(
            "action IN ('push','pull','rollback','upgrade','resolve','defer')",
            name="ck_sync_action",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    skill_id: Mapped[str | None] = mapped_column(ForeignKey("skills.id"))
    runtime_id: Mapped[str | None] = mapped_column(ForeignKey("runtimes.id"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    from_state: Mapped[str | None] = mapped_column(Text)
    to_state: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSON)  # 变更文件列表 / skipped / preserved 明细
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    by: Mapped[str | None] = mapped_column(Text)  # 操作者标识


# ---------------------------------------------------------------- 来源与质量


class UpstreamSource(Base):
    __tablename__ = "upstream_sources"
    __table_args__ = (
        CheckConstraint("type IN ('git','marketplace','url','local')", name="ck_upstream_type"),
        CheckConstraint(
            "status IN ('current','outdated','error','unknown')", name="ck_upstream_status"
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    ref: Mapped[str | None] = mapped_column(Text)  # 分支 / tag
    base_version: Mapped[str | None] = mapped_column(Text)
    upstream_hash: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")


class SkillCheck(Base):
    """检查结果（NFR-9 审计）。

    E-02.6：折旧三维与门禁 findings 同表，用 `scope` 区分 —— `gate` 为门禁逐条规则，
    `decay` 为折旧维度，`value` 存原始测量值 JSON 供 UI 下钻（NFR-10.3）。
    E-01.3：decay 行 ok→info / degraded→warn / unknown→**unknown**，
    「未知 N 项」就是 `scope='decay' AND level='unknown'` 的行数，必须数得出来。
    """

    __tablename__ = "skill_checks"
    __table_args__ = (
        CheckConstraint(
            "level IN ('error','warn','info','unknown')", name="ck_check_level"
        ),
        CheckConstraint("scope IN ('gate','decay')", name="ck_check_scope"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), nullable=False)
    version_id: Mapped[str | None] = mapped_column(ForeignKey("skill_versions.id"))
    scope: Mapped[str] = mapped_column(Text, nullable=False, default="gate",
                                      server_default="gate")
    rule: Mapped[str] = mapped_column(Text, nullable=False)  # 见 core/algorithm/checks.py
    level: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str | None] = mapped_column(Text)  # JSON：decay 原始测量值 / 门禁附加信息
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class SkillUsageDaily(Base):
    """日聚合表（N-01.3 / N-11.6，本地与云端**同构**）。

    冲突键 `(device_id, skill_id, date, via)` 是云端合并与补报去重的落点（V2-T9）：
    `INSERT ... ON CONFLICT DO UPDATE SET count = count + excluded.count`，
    所以这里必须声明同名唯一约束。
    """

    __tablename__ = "skill_usage_daily"
    __table_args__ = (
        UniqueConstraint("device_id", "skill_id", "date", "via", name="uq_usage_daily"),
        Index("ix_usage_daily_skill", "skill_id", "date"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    device_id: Mapped[str] = mapped_column(Text, nullable=False)   # 本地固定 'local'
    skill_id: Mapped[str] = mapped_column(Text, nullable=False)    # skill slug
    date: Mapped[date] = mapped_column(Date, nullable=False)       # 聚合日（UTC）
    via: Mapped[str] = mapped_column(Text, nullable=False)         # mcp / web / cli / daemon
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)   # L2 缺省 0
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms_p50: Mapped[int | None] = mapped_column(Integer)
    duration_ms_p95: Mapped[int | None] = mapped_column(Integer)
    by_op: Mapped[str | None] = mapped_column(Text)   # JSON：{"get_skill": 3, "sync_push": 1}
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class SkillUsageEvent(Base):
    """本地事件表（N-11.1）：**聚合的唯一事实源**，保留 30 天滚动清理。

    不上云（N-01.5 / N-11.6）：`param_digest` 只是参数摘要，正文与参数永不入此表。
    """

    __tablename__ = "skill_usage_events"
    __table_args__ = (
        Index("ix_usage_events_skill_at", "skill_id", "at"),
        Index("ix_usage_events_session", "session_id", "at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)   # runtime + session 组合键
    runtime: Mapped[str] = mapped_column(Text, nullable=False, default="")
    skill_id: Mapped[str] = mapped_column(Text, nullable=False)
    op: Mapped[str] = mapped_column(Text, nullable=False)
    via: Mapped[str] = mapped_column(Text, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    param_digest: Mapped[str | None] = mapped_column(Text)   # 参数摘要（不上云）
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class SkillSimilarity(Base):
    __tablename__ = "skill_similarity"
    __table_args__ = (
        CheckConstraint("skill_a_id < skill_b_id", name="ck_similarity_pair"),  # 防重复对
    )

    skill_a_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), primary_key=True)
    skill_b_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), primary_key=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)  # 0–1
    method: Mapped[str] = mapped_column(Text, nullable=False, default="tfidf")


# ---------------------------------------------------------------- 协作（仅云端）


class ChangeRequest(Base):
    """协作 CR（FR-2.3.3 / FR-3.7 / FR-3.16）。

    - CR 自身 + CrComment 表即为审计源；**应用层不提供改写 / 删除接口**（FR-3.16）。
    - 状态机：open → closed（reviewer 驳回）/ merged（owner 合并）；merged 后写回
      canonical 的 `head_version`，触发下一阶段团队 binding push。
    """

    __tablename__ = "change_requests"
    __table_args__ = (
        CheckConstraint("status IN ('open','merged','closed')", name="ck_cr_status"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), nullable=False)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), nullable=False)
    base_version: Mapped[str | None] = mapped_column(Text)  # 展示用：基线版本号
    target_commit: Mapped[str | None] = mapped_column(Text)  # git commit：合并的最终事实
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    author_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    reviewer_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class CrComment(Base):
    __tablename__ = "cr_comments"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    cr_id: Mapped[str] = mapped_column(ForeignKey("change_requests.id"), nullable=False)
    author_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
