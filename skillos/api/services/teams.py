"""P6 协作服务：team 权限阶梯 / CR 状态机。

边界（红线）：
- 权限中间件按 role 等级校验；任何角色越权 → 403 INSUFFICIENT_ROLE
- CR 状态机：open → merged | closed，单向不可逆（FR-3.16 不可变审计）
- 评论只增不删（应用层无删除接口，FR-3.16）
"""

from __future__ import annotations

from fastapi import HTTPException

from skillos.store.cloud_repo import CloudRepo

# 角色阶梯：view ≤ write ≤ admin
_ROLE_RANK = {"viewer": 1, "member": 2, "owner": 3}


def require_team_role(
    repo: CloudRepo, team_id: str, user_id: str, *, min_role: str = "viewer"
) -> tuple[str, str]:
    """权限中间件：返回 (user_id, role)；越权 403。

    - viewer：只读（成员/技能/CR 列表）
    - member：提交 CR / 评论
    - owner：成员管理 / 审查 CR / 改名团队 / 合并
    """
    m = repo.get_membership(team_id, user_id)
    if m is None:
        raise HTTPException(status_code=403, detail="NOT_MEMBER")
    if _ROLE_RANK.get(m.role, 0) < _ROLE_RANK.get(min_role, 0):
        raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
    return user_id, m.role


def require_admin(repo: CloudRepo, team_id: str, user_id: str) -> tuple[str, str]:
    """owner 专属操作（成员管理 / 改名）。"""
    return require_team_role(repo, team_id, user_id, min_role="owner")


def require_writer(repo: CloudRepo, team_id: str, user_id: str) -> tuple[str, str]:
    """member 及以上（含 owner）：写 CR / 评论。"""
    return require_team_role(repo, team_id, user_id, min_role="member")


def require_viewer(repo: CloudRepo, team_id: str, user_id: str) -> tuple[str, str]:
    """任意 team 成员。"""
    return require_team_role(repo, team_id, user_id, min_role="viewer")


# CR 状态机（FR-3.16 不可变审计）

CR_STATUSES = ("open", "merged", "closed")
CR_TERMINAL = ("merged", "closed")


def is_terminated(status: str) -> bool:
    """CR 一旦进入 merged/closed 即终结，再次审查 / 状态变更应拒绝。"""
    return status in CR_TERMINAL