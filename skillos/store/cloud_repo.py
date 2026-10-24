"""云端数据访问层（04 §3.2 表结构，SQLite/Postgres 同构）。

与本地 index_repo 的分工：
- index_repo：本地 SQLite 缓存，可重建（T6）
- cloud_repo：云端真值（用户/配对/token/审计），含漂移上报 upsert
云端不缓存文件内容（FR-3.12），只存元数据与哈希。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from skillos.core.constants import DRIFT_SEVERITY
from skillos.store import models

PAIRING_TTL = timedelta(minutes=10)
PAIRING_MAX_FAILS = 5
PAIRING_LOCK_S = 60


def _now() -> datetime:
    # SQLite DateTime 列不带时区（SQLAlchemy 默认 naive）—— 全层统一 naive UTC，
    # 避免写 offset-aware 比较 offset-naive 抛 TypeError
    return datetime.now(UTC).replace(tzinfo=None)


class CloudRepo:
    """每个请求一个 Session 由调用方管理；本类只做无状态查询/写入。"""

    def __init__(self, session: Session) -> None:
        self.s = session

    # ---------------------------------------------------------------- 用户

    def create_user(self, email: str, name: str | None, password_hash: str) -> models.User:
        user = models.User(
            id=f"usr_{secrets.token_hex(8)}",
            email=email,
            name=name,
            password_hash=password_hash,
        )
        self.s.add(user)
        self.s.commit()
        return user

    def get_user_by_email(self, email: str) -> models.User | None:
        return self.s.scalar(select(models.User).where(models.User.email == email))

    def get_user(self, user_id: str) -> models.User | None:
        return self.s.get(models.User, user_id)

    # ---------------------------------------------------------------- 配对

    def issue_pairing_code(self, user_id: str) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        self.s.add(models.PairingCode(
            code=code, user_id=user_id, expires_at=_now() + PAIRING_TTL,
        ))
        self.s.commit()
        return code

    def check_scope(self, scope: str) -> tuple[bool, int]:
        """单个 scope 是否放行（FR-3.15）。返回 (允许, retry_after_s)。"""
        att = self.s.get(models.PairingAttempt, scope)
        if att and att.locked_until and att.locked_until > _now():
            return False, int((att.locked_until - _now()).total_seconds()) + 1
        return True, 0

    def check_rate_limit(self, user_id: str, ip: str) -> tuple[bool, int]:
        """user 与 ip 双维度检查，任一锁定即拒绝。"""
        for scope in (f"user:{user_id}", f"ip:{ip}"):
            allowed, retry = self.check_scope(scope)
            if not allowed:
                return False, retry
        return True, 0

    def record_scope_failure(self, scope: str) -> bool:
        """单 scope 失败计数 +1；连续 5 次锁 60s 并清零计数。返回是否触发锁定。"""
        att = self.s.get(models.PairingAttempt, scope)
        if att is None:
            att = models.PairingAttempt(scope=scope, failed_count=0)
            self.s.add(att)
        att.failed_count += 1
        att.updated_at = _now()
        locked = False
        if att.failed_count >= PAIRING_MAX_FAILS:
            att.locked_until = _now() + timedelta(seconds=PAIRING_LOCK_S)
            att.failed_count = 0
            locked = True
        self.s.commit()
        return locked

    def record_pairing_failure(self, user_id: str, ip: str) -> bool:
        """双维度失败计数（04 §5.1 失败路径）。"""
        locked = False
        for scope in (f"user:{user_id}", f"ip:{ip}"):
            if self.record_scope_failure(scope):
                locked = True
        return locked

    def record_pairing_success(self, user_id: str, ip: str) -> None:
        """校验成功清零两个 scope 计数（04 §5.1 失败路径表）。"""
        for scope in (f"user:{user_id}", f"ip:{ip}"):
            att = self.s.get(models.PairingAttempt, scope)
            if att:
                att.failed_count = 0
                att.locked_until = None
                att.updated_at = _now()
        self.s.commit()

    def peek_pairing_code(self, code: str) -> models.PairingCode | None:
        """只查不消费：锁定检查需要 user_id，须在校验前知道码归属。"""
        pc = self.s.get(models.PairingCode, code)
        if pc is None or pc.used_at is not None or pc.expires_at < _now():
            return None
        return pc

    def consume_pairing_code(self, code: str) -> models.PairingCode | None:
        """校验码有效（未过期未用）并原子标记已用；无效返回 None。"""
        pc = self.s.get(models.PairingCode, code)
        if pc is None or pc.used_at is not None or pc.expires_at < _now():
            return None
        pc.used_at = _now()
        self.s.commit()
        return pc

    # ---------------------------------------------------------------- token / 设备

    def issue_agent_token(
        self, user_id: str, agent_id: str, token_id: str, token_hash: str,
        platform: str | None = None, name: str | None = None,
    ) -> models.AgentToken:
        tok = models.AgentToken(
            id=token_id, user_id=user_id, agent_id=agent_id,
            token_hash=token_hash, platform=platform, name=name,
        )
        self.s.add(tok)
        self.s.commit()
        return tok

    def find_token_by_hash(self, token_hash: str) -> models.AgentToken | None:
        """有效 = 哈希匹配且未撤销。"""
        tok = self.s.scalar(
            select(models.AgentToken).where(models.AgentToken.token_hash == token_hash)
        )
        if tok is None or tok.revoked_at is not None:
            return None
        return tok

    def touch_token(self, token_id: str) -> None:
        tok = self.s.get(models.AgentToken, token_id)
        if tok:
            tok.last_seen_at = _now()
            self.s.commit()

    def list_devices(self, user_id: str) -> list[models.AgentToken]:
        return list(self.s.scalars(
            select(models.AgentToken)
            .where(models.AgentToken.user_id == user_id)
            .order_by(models.AgentToken.created_at)
        ))

    def revoke_token(self, user_id: str, token_id: str) -> models.AgentToken | None:
        """撤销（FR-1.2.8）：置 revoked_at，不物理删除。"""
        tok = self.s.get(models.AgentToken, token_id)
        if tok is None or tok.user_id != user_id:
            return None
        tok.revoked_at = _now()
        self.s.commit()
        return tok

    # ---------------------------------------------------------------- Skill / 漂移

    def upsert_skill(self, slug: str, name: str, description: str | None,
                     user_id: str) -> models.Skill:
        """daemon 上报时按 slug upsert（漂移矩阵行的锚点）。"""
        skill = self.s.scalar(select(models.Skill).where(models.Skill.slug == slug))
        if skill is None:
            skill = models.Skill(
                id=f"skl_{secrets.token_hex(8)}", slug=slug, name=name,
                description=description, source_kind="local", owner_id=user_id,
            )
            self.s.add(skill)
        else:
            skill.name = name or skill.name
            skill.description = description or skill.description
            skill.updated_at = _now()
        self.s.commit()
        return skill

    def upsert_runtime(self, key: str, user_id: str) -> models.Runtime:
        rt = self.s.scalar(select(models.Runtime).where(models.Runtime.key == key))
        if rt is None:
            rt = models.Runtime(
                id=f"rt_{secrets.token_hex(6)}", key=key, adapter_key=key,
                root_path=f"~/.{key}/skills", owner_id=user_id,
            )
            self.s.add(rt)
            self.s.commit()
        return rt

    def upsert_binding(self, skill_id: str, runtime_id: str, status: str,
                       installed_version: str | None = None) -> models.RuntimeBinding:
        b = self.s.scalar(select(models.RuntimeBinding).where(
            models.RuntimeBinding.skill_id == skill_id,
            models.RuntimeBinding.runtime_id == runtime_id,
        ))
        if b is None:
            b = models.RuntimeBinding(
                id=f"rb_{secrets.token_hex(6)}", skill_id=skill_id,
                runtime_id=runtime_id, status=status,
                baseline_source="distributed", installed_version=installed_version,
            )
            self.s.add(b)
        else:
            b.status = status
            b.installed_version = installed_version or b.installed_version
            b.last_synced_at = _now()
        self.s.commit()
        return b

    def upsert_matrix(self, user_id: str, cells: list[dict]) -> None:
        """daemon 矩阵（status 任务结果 / drift_changed 事件）→ 云端缓存。

        徽标（has_untracked_assets / adapter_outdated）不落库（T6 派生字段约束），
        只随 live 响应透传给前端；离线降级视图自然无徽标。
        """
        for cell in cells:
            slug = str(cell.get("slug") or "")
            runtime_key = str(cell.get("runtime_key") or "")
            state = str(cell.get("state") or "").lower()
            if not (slug and runtime_key and state):
                continue
            skill = self.upsert_skill(
                slug, str(cell.get("name") or slug), cell.get("description"), user_id,
            )
            rt = self.upsert_runtime(runtime_key, user_id)
            self.upsert_binding(skill.id, rt.id, state, cell.get("installed_version"))

    def get_skill_by_slug(self, slug: str) -> models.Skill | None:
        return self.s.scalar(select(models.Skill).where(models.Skill.slug == slug))

    def record_checks(self, skill_id: str, findings: list[dict]) -> None:
        """门禁结果落库（FR-1.0.11 / NFR-9）：先清旧再写新，保持「最新一次」语义。

        只清 `scope='gate'` 的行 —— 折旧（decay）结果由 `record_decay_checks` 单独维护，
        两类结果同表但互不覆盖（E-02.6）。
        """
        for row in self.s.scalars(
            select(models.SkillCheck).where(
                models.SkillCheck.skill_id == skill_id,
                models.SkillCheck.scope == "gate",
            )
        ):
            self.s.delete(row)
        for f in findings:
            level = str(f.get("level") or "info").lower()
            if level not in ("error", "warn", "info"):
                level = "info"
            self.s.add(models.SkillCheck(
                id=f"chk_{secrets.token_hex(8)}", skill_id=skill_id, scope="gate",
                rule=str(f.get("rule") or ""), level=level,
                message=str(f.get("message") or ""),
            ))
        self.s.commit()

    def record_decay_checks(self, skill_id: str, rows: list[dict]) -> int:
        """折旧三维落库（E-02.6）：一行一个维度，`value` 存原始测量值供 UI 下钻。"""
        for row in self.s.scalars(
            select(models.SkillCheck).where(
                models.SkillCheck.skill_id == skill_id,
                models.SkillCheck.scope == "decay",
            )
        ):
            self.s.delete(row)
        for row in rows:
            self.s.add(models.SkillCheck(
                id=f"chk_{secrets.token_hex(8)}", skill_id=skill_id, scope="decay",
                rule=str(row.get("rule") or ""), level=str(row.get("level") or "unknown"),
                message=str(row.get("message") or ""), value=row.get("value"),
            ))
        self.s.commit()
        return len(rows)

    # ---------------------------------------------------------------- Team & 协作（P6，FR-3.7）

    def create_team(self, name: str, owner_id: str) -> models.Team:
        team = models.Team(id=f"tm_{secrets.token_hex(8)}", name=name, owner_id=owner_id)
        self.s.add(team)
        # 创建者自动为 owner（FR-2.3.4）
        self.s.add(models.TeamMember(team_id=team.id, user_id=owner_id, role="owner"))
        self.s.commit()
        return team

    def get_team(self, team_id: str) -> models.Team | None:
        return self.s.get(models.Team, team_id)

    def list_user_teams(self, user_id: str) -> list[models.Team]:
        rows = self.s.execute(
            select(models.Team, models.TeamMember)
            .join(models.TeamMember, models.TeamMember.team_id == models.Team.id)
            .where(models.TeamMember.user_id == user_id)
            .order_by(models.Team.created_at)
        ).all()
        return [t for t, _m in rows]

    def rename_team(self, team_id: str, name: str) -> models.Team | None:
        team = self.s.get(models.Team, team_id)
        if team is None:
            return None
        team.name = name
        self.s.commit()
        return team

    def add_member(self, team_id: str, user_id: str, role: str) -> models.TeamMember:
        existing = self.s.get(models.TeamMember, (team_id, user_id))
        if existing:
            existing.role = role
            self.s.commit()
            return existing
        m = models.TeamMember(team_id=team_id, user_id=user_id, role=role)
        self.s.add(m)
        self.s.commit()
        return m

    def remove_member(self, team_id: str, user_id: str) -> bool:
        m = self.s.get(models.TeamMember, (team_id, user_id))
        if m is None:
            return False
        if m.role == "owner":
            return False  # owner 不可移除；先转交
        self.s.delete(m)
        self.s.commit()
        return True

    def get_membership(self, team_id: str, user_id: str) -> models.TeamMember | None:
        return self.s.get(models.TeamMember, (team_id, user_id))

    def list_members(self, team_id: str) -> list[models.TeamMember]:
        return list(self.s.scalars(
            select(models.TeamMember).where(models.TeamMember.team_id == team_id)
        ))

    def attach_skill_to_team(self, skill_id: str, team_id: str) -> models.Skill:
        skill = self.s.get(models.Skill, skill_id)
        if skill is None:
            raise KeyError(skill_id)
        skill.team_id = team_id
        skill.visibility = "team"
        self.s.commit()
        return skill

    def detach_skill_from_team(self, skill_id: str) -> models.Skill:
        skill = self.s.get(models.Skill, skill_id)
        if skill is None:
            raise KeyError(skill_id)
        skill.team_id = None
        skill.visibility = "private"
        self.s.commit()
        return skill

    def list_team_skills(self, team_id: str) -> list[models.Skill]:
        return list(self.s.scalars(
            select(models.Skill).where(models.Skill.team_id == team_id)
        ))

    # ---------------------------------------------------------------- Change Request

    def create_change_request(
        self, team_id: str, skill_id: str, title: str, body: str | None,
        author_id: str, base_version: str | None = None,
        target_commit: str | None = None,
    ) -> models.ChangeRequest:
        cr = models.ChangeRequest(
            id=f"cr_{secrets.token_hex(8)}", team_id=team_id, skill_id=skill_id,
            title=title, body=body, author_id=author_id,
            base_version=base_version, target_commit=target_commit,
        )
        self.s.add(cr)
        self.s.commit()
        return cr

    def get_change_request(self, cr_id: str) -> models.ChangeRequest | None:
        return self.s.get(models.ChangeRequest, cr_id)

    def list_team_crs(self, team_id: str, status: str | None = None) -> list[models.ChangeRequest]:
        q = select(models.ChangeRequest).where(models.ChangeRequest.team_id == team_id)
        if status:
            q = q.where(models.ChangeRequest.status == status)
        return list(self.s.scalars(q.order_by(models.ChangeRequest.created_at)))

    def set_cr_status(self, cr_id: str, status: str,
                      reviewer_id: str) -> models.ChangeRequest | None:
        if status not in ("open", "merged", "closed"):
            raise ValueError(status)
        cr = self.s.get(models.ChangeRequest, cr_id)
        if cr is None:
            return None
        if cr.status != "open":
            raise ValueError("CR 已终结，不可再次审查（FR-3.16 不可变审计）")
        cr.status = status
        cr.reviewer_id = reviewer_id
        cr.updated_at = _now()
        self.s.commit()
        return cr

    def add_cr_comment(self, cr_id: str, author_id: str, body: str) -> models.CrComment:
        if not body.strip():
            raise ValueError("评论为空")
        c = models.CrComment(
            id=f"cm_{secrets.token_hex(8)}", cr_id=cr_id, author_id=author_id,
            body=body.strip(),
        )
        self.s.add(c)
        self.s.commit()
        return c

    def list_cr_comments(self, cr_id: str) -> list[models.CrComment]:
        return list(self.s.scalars(
            select(models.CrComment).where(models.CrComment.cr_id == cr_id)
            .order_by(models.CrComment.at)
        ))

    # ---------------------------------------------------------------- 管理者视图（FR-3.17）

    def team_dashboard(self, team_id: str) -> dict:
        """团队级覆盖度 / 僵尸 / 成员贡献（复用本机聚合口径 manager_stats）。

        - coverage：team 内 skill 的所有 binding 单元格中 SYNCED 占比
        - zombie：聚合后 ORPHANED / UNMANAGED 的 skill 数（canonical 侧已无价值）
        - contribution：按 skill owner 维度计 skill 数 + commit 数（skill_versions.created_by）
        """
        skills = self.list_team_skills(team_id)
        skill_ids = [s.id for s in skills]
        if not skill_ids:
            return {"total_skills": 0, "total_cells": 0, "synced_cells": 0,
                    "coverage_pct": 0.0, "zombie_pct": 0.0, "health": {},
                    "contribution": []}
        runtimes = {r.id: r for r in self.s.scalars(select(models.Runtime))}
        bindings = list(self.s.scalars(
            select(models.RuntimeBinding).where(
                models.RuntimeBinding.skill_id.in_(skill_ids))
        ))
        total_cells = len(bindings)
        synced_cells = sum(1 for b in bindings if b.status == "synced")
        # 按 skill 聚合取最严重（FR-2.1.9）
        by_skill: dict[str, list[str]] = {}
        for b in bindings:
            by_skill.setdefault(b.skill_id, []).append(b.status)
        aggregates = {sid: aggregate_skill_status(v) for sid, v in by_skill.items()}
        zombie = sum(1 for sid in skill_ids if aggregates.get(sid) in ("ORPHANED", "UNMANAGED"))
        # 健康度按聚合分布
        health: dict[str, int] = {}
        for s in aggregates.values():
            health[s] = health.get(s, 0) + 1
        # 成员贡献：按 skill owner_id 计数 skill + version
        contrib: dict[str, dict] = {}
        for s in skills:
            d = contrib.setdefault(s.owner_id or "unowned",
                                   {"user_id": s.owner_id, "skill_count": 0, "version_count": 0})
            d["skill_count"] += 1
        ver_count = {row.created_by or "unowned": row.n for row in self.s.execute(
            select(models.SkillVersion.created_by,
                   func.count().label("n"))  # type: ignore[has-type]
            .where(models.SkillVersion.skill_id.in_(skill_ids))
            .group_by(models.SkillVersion.created_by)
        ).all()}
        for uid, n in ver_count.items():
            contrib.setdefault(uid, {"user_id": uid, "skill_count": 0,
                                     "version_count": 0})["version_count"] = n
        return {
            "total_skills": len(skill_ids),
            "total_cells": total_cells,
            "synced_cells": synced_cells,
            "coverage_pct": round(100 * synced_cells / total_cells, 1) if total_cells else 0.0,
            "zombie_pct": round(100 * zombie / len(skill_ids), 1) if skill_ids else 0.0,
            "health": health,
            "contribution": sorted(contrib.values(),
                                   key=lambda d: (d["skill_count"], d["version_count"]),
                                   reverse=True),
            "runtimes": len(runtimes),
        }

    # ---------------------------------------------------------------- 通用 list_bindings

    def list_bindings(
        self, user_id: str
    ) -> list[tuple[models.Skill, models.RuntimeBinding, models.Runtime]]:
        """该用户全部 skill 的 binding + runtime key（漂移矩阵数据源）。"""
        skills = {
            s.id: s for s in self.s.scalars(
                select(models.Skill).where(models.Skill.owner_id == user_id)
            )
        }
        if not skills:
            return []
        runtimes = {
            r.id: r for r in self.s.scalars(select(models.Runtime))
        }
        bindings = self.s.scalars(
            select(models.RuntimeBinding).where(
                models.RuntimeBinding.skill_id.in_(skills.keys())
            )
        )
        out = []
        for b in bindings:
            rt = runtimes.get(b.runtime_id)
            if rt is not None:
                out.append((skills[b.skill_id], b, rt))
        return out

    def record_sync(self, action: str, skill_id: str | None, runtime_id: str | None,
                    detail: dict | None, by: str) -> None:
        self.s.add(models.SyncRecord(
            id=f"rec_{secrets.token_hex(8)}", action=action, skill_id=skill_id,
            runtime_id=runtime_id, detail=detail, by=by,
        ))
        self.s.commit()

    # ---------------------------------------------------------------- Settings 区块
    # FR-2.4.10 / T26：custom Runtime 的 CRUD。
    # 写操作全部在云端 handler 直接调（无需 daemon task）：Runtime 表是 cloud
    # 端 db，daemon 不连 cloud db；custom 条目在 daemon 端没有副作用。
    # discover_only 标志：custom 新建时默认 True（必须显式置 False 才进正常使用）
    # —— 显式状态机（T25 红线："路径填了但忘了改标志"被默认行为吞掉是禁止的）。

    def list_custom_runtimes(self, owner_id: str) -> list[models.Runtime]:
        """该 user 的全部 custom Runtime（含 discover_only=True 待填路径的）。
        Settings 页"已纳管 Runtime 列表"的 custom 段数据源。
        """
        return list(self.s.scalars(
            select(models.Runtime)
            .where(models.Runtime.owner_id == owner_id)
            .where(models.Runtime.adapter_key == "custom")
        ))

    def get_runtime_by_key(self, key: str) -> models.Runtime | None:
        return self.s.scalar(
            select(models.Runtime).where(models.Runtime.key == key)
        )

    def create_custom_runtime(
        self, *, key: str, name: str | None, root_path: str,
        scope: str = "user", owner_id: str, discover_only: bool = False,
    ) -> models.Runtime:
        """创建 custom Runtime 条目（FR-1.0.34 / T25）。

        key 必须 `custom:<name>` 命名空间（FR-1.0.25），避免与 builtin 冲突。
        discover_only 默认 False = "已纳管 + 路径已填好"；用户也可先建空条目
        （discover_only=True）再到 Settings 编辑路径（FR-1.0.34 的"先创建后填"路径）。
        """
        if not key.startswith("custom:"):
            raise ValueError(f"custom Runtime key 必须 'custom:' 命名空间: {key!r}")
        if self.s.scalar(select(models.Runtime).where(models.Runtime.key == key)):
            raise ValueError(f"Runtime key 已存在: {key!r}")
        rt = models.Runtime(
            id=f"rt_{secrets.token_hex(6)}", key=key, name=name or key,
            adapter_key="custom", root_path=root_path, scope=scope,
            enabled=True, owner_id=owner_id, discover_only=discover_only,
        )
        self.s.add(rt)
        self.s.commit()
        return rt

    def update_custom_runtime_path(
        self, key: str, root_path: str, discover_only: bool | None = None,
    ) -> models.Runtime | None:
        """编辑 custom Runtime 路径（FR-1.0.25 / FR-1.0.33）。

        discover_only 显式传：None = 不改，True / False = 改。T25 红线：不能
        "改路径就自动 discover_only=False" —— 用户必须显式确认。
        """
        rt = self.get_runtime_by_key(key)
        if rt is None or rt.adapter_key != "custom":
            return None
        rt.root_path = root_path
        if discover_only is not None:
            rt.discover_only = discover_only
        self.s.commit()
        return rt

    def delete_custom_runtime(self, key: str) -> bool:
        """删除 custom Runtime（FR-1.0.25 / NFR-11.4）。仅 custom 条目可删（不删 builtin）。"""
        rt = self.get_runtime_by_key(key)
        if rt is None or rt.adapter_key != "custom":
            return False
        # 同时清理 binding（避免悬挂引用；deletion cascade 由 ORM 关系决定）
        self.s.execute(
            delete(models.RuntimeBinding).where(
                models.RuntimeBinding.runtime_id == rt.id
            )
        )
        self.s.delete(rt)
        self.s.commit()
        return True


def aggregate_skill_status(states: list[str]) -> str:
    """FR-2.1.9：组内取最严重；部分 SYNCED ≠ 全 SYNCED → PARTIAL。

    输入为小写状态串（DB 约束）；输出大写，供 API / 前端。
    - 含行动类状态（conflict/modified/outdated/missing）→ 取最严重（需用户处理）
    - 仅混合 synced + unmanaged/orphaned（信息类）→ PARTIAL（部分分发 ≠ 已关闭，07 N11）
    """
    if not states:
        return "MISSING"
    upper = [s.upper() for s in states]
    actionable = [s for s in upper if s in ("CONFLICT", "MODIFIED", "OUTDATED", "MISSING")]
    if actionable:
        return max(upper, key=lambda s: DRIFT_SEVERITY.get(s, 0))
    if all(s == "SYNCED" for s in upper):
        return "SYNCED"
    if "SYNCED" in upper:
        return "PARTIAL"  # 部分 Runtime 已分发 + 其余信息类未纳管（07 N11）
    return max(upper, key=lambda s: DRIFT_SEVERITY.get(s, 0))


def drift_summary(pairs) -> dict[str, int]:
    """按 Skill 聚合后的状态计数（UI 广播 / ManagerPanel 共用口径）。"""
    by_skill: dict[str, list[str]] = {}
    for skill, binding, _rt in pairs:
        by_skill.setdefault(skill.slug, []).append(binding.status)
    summary: dict[str, int] = {}
    for states in by_skill.values():
        s = aggregate_skill_status(states)
        summary[s] = summary.get(s, 0) + 1
    return summary


def manager_stats(pairs) -> dict:
    """双读者下半部（FR-2.1.1 决策者读法）：覆盖度 / 僵尸占比 / 健康度分布。

    - 覆盖度 = SYNCED 格 / 全部格子（Skill × Runtime 中的适用格）
    - 僵尸 Skill = 聚合后 ORPHANED 或 UNMANAGED（canonical 侧已无价值）
    """
    total_cells = len(pairs)
    synced_cells = sum(1 for _s, b, _r in pairs if b.status == "synced")
    by_skill: dict[str, list[str]] = {}
    for skill, binding, _rt in pairs:
        by_skill.setdefault(skill.slug, []).append(binding.status)
    aggregates = [aggregate_skill_status(v) for v in by_skill.values()]
    zombie = sum(1 for a in aggregates if a in ("ORPHANED", "UNMANAGED"))
    total_skills = len(aggregates)
    return {
        "total_skills": total_skills,
        "total_cells": total_cells,
        "coverage_pct": round(100 * synced_cells / total_cells, 1) if total_cells else 0.0,
        "zombie_pct": round(100 * zombie / total_skills, 1) if total_skills else 0.0,
        "health": drift_summary(pairs),
    }
