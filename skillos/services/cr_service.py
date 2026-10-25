"""Change Request 状态机（E-05 / `docs/12` §7.1）。仅云端。

为什么单独成文件：状态迁移、角色门槛、门禁前置、STALE 判定散落在路由里时，
「哪些边存在」会退化成 if-else 的隐式属性 —— 改一处漏一处，且无法静态列举。
这里只有**一张显式迁移表 + 一个纯函数**：路由只负责取事实（daemon 的门禁与
canonical HEAD）并落库，判定全在 `transition()`。

四个口径点（对应 E-05.1~3 / 5~6）：

- **终态无出边**：`merged/rejected/closed` 不在迁移表左列 ⇒ 回跳天然被拒，
  不需要在每个 if 里再记一次「已终结」；
- **门禁前置**（E-05.2）：`approved→merge` 必须拿到 gate 事实，`has_error` 一律
  拒绝，且 CR 退回 `open`（`GATE_FAILURE_REVERT`：唯一允许的回跳，显式执行，
  **不进迁移表** —— 它是失败处置，不是合法业务边）；
- **STALE**（E-05.3）：合并要求 `target_commit` 就是该 Skill 当前的 canonical HEAD，
  否则 `409 STALE` 让用户刷新重做，绝不静默把旧提交当新事实；
- **审计**：每次迁移都落 `sync_records` + 关键失败写系统评论（`author_id=None`），
  评论只增不删（FR-3.16）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 迁移表

# (当前状态, 事件) → 新状态。终态（merged/rejected/closed）只做右值，不做左值。
CR_TRANSITIONS: dict[tuple[str, str], str] = {
    ("open", "approve"): "approved",
    ("open", "reject"): "rejected",
    ("open", "close"): "closed",
    ("approved", "merge"): "merged",
    ("approved", "close"): "closed",
}

TERMINAL_STATUSES: frozenset[str] = frozenset({"merged", "rejected", "closed"})

# 审查类事件：owner / member 均可（viewer → 403，E-05.1）
REVIEW_EVENTS: frozenset[str] = frozenset({"approve", "reject"})
# 处置类事件：提交者本人或 owner
AUTHOR_OR_OWNER_EVENTS: frozenset[str] = frozenset({"merge", "close"})
REASON_REQUIRED_EVENTS: frozenset[str] = frozenset({"reject"})

# E-05.2：门禁失败后的落点。**唯一**允许的回跳，仅由 `transition()` 抛出的
# `GATE_FAILED` 分支触发，不进入迁移表（否则「合并失败」会被当成一条合法业务边）。
GATE_FAILURE_REVERT = "open"


class CrError(Exception):
    """迁移被拒。`code` 直接映射到 API 错误信封，`status` 是建议 HTTP 码。

    `detail` 只放**可直接展示**的事实（如 gate findings）——错误处理不许猜。
    """

    def __init__(
        self, code: str, message: str, *, status: int = 409,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}


# ---------------------------------------------------------------- 事实与结果


@dataclass(frozen=True)
class MergeFacts:
    """`approved→merge` 需要的两项外部事实（都来自 daemon，云端不自造）。

    - `gate_has_error` / `gate_findings`：与 push 前置同一实现（NFR-10.5）；
    - `canonical_head`：该 Skill 当前 canonical 最新 commit；`None` = 读不到。
    """

    gate_has_error: bool
    gate_findings: tuple[dict, ...]
    canonical_head: str | None


@dataclass(frozen=True)
class CrTransition:
    """一次迁移的结论（纯数据，落库由调用方执行）。"""

    event: str
    from_status: str
    to_status: str
    actor_id: str
    reason: str | None = None
    facts: MergeFacts | None = None
    comments: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------- 判定


def allowed_events(status: str) -> tuple[str, ...]:
    """当前状态允许的事件（按迁移表推导，前端按钮/测试都不用另抄一份）。"""
    return tuple(event for (src, event) in CR_TRANSITIONS if src == status)


def next_status(status: str, event: str) -> str:
    """取迁移终态；无边即拒绝（终态与非法事件分开报，便于前端区分）。"""
    target = CR_TRANSITIONS.get((status, event))
    if target is not None:
        return target
    if status in TERMINAL_STATUSES:
        raise CrError(
            "CR_TERMINAL", f"CR 已终结（{status}），不可再变更（FR-3.16）",
            status=409,
        )
    raise CrError(
        "INVALID_TRANSITION", f"{status} 不接受事件 {event}（先审查通过才能合并）",
        status=409, detail={"allowed_events": list(allowed_events(status))},
    )


def assert_role(
    event: str, *, actor_id: str, actor_role: str, author_id: str | None,
) -> None:
    """角色门槛（E-05.1）。viewer 连审查都不行，处置类事件限提交者/owner。"""
    if event in REVIEW_EVENTS:
        if actor_role not in ("owner", "member"):
            raise CrError("FORBIDDEN", "viewer 不能审查 CR（E-05.1）", status=403)
        return
    if event in AUTHOR_OR_OWNER_EVENTS:
        if actor_role != "owner" and actor_id != author_id:
            raise CrError(
                "FORBIDDEN", "仅提交者本人或 owner 可执行该操作（E-05.1）", status=403,
            )


def _gate_comment(findings: tuple[dict, ...]) -> str:
    """GATE_FAILED 明细（E-05.2）：逐条规则列出来，别让用户去别处猜。"""
    if not findings:
        return "系统：合并前置门禁未通过（GATE_FAILED），但未返回具体规则明细。"
    lines = [
        f"- {f.get('rule', '?')} [{f.get('level', '?')}] {f.get('message', '')}".rstrip()
        for f in findings
        if str(f.get("level")) == "error"
    ]
    body = "\n".join(lines) or "（规则未标注级别）"
    return f"系统：合并前置门禁未通过（GATE_FAILED），CR 已退回 open。\n{body}"


def _stale_comment(target_commit: str, canonical_head: str) -> str:
    return (
        "系统：合并被拒（STALE）—— canonical 已前进，请刷新后重新提交。\n"
        f"- CR 目标提交：{target_commit[:12]}\n"
        f"- 当前 canonical：{canonical_head[:12]}"
    )


def transition(
    *,
    status: str,
    event: str,
    actor_id: str,
    actor_role: str,
    author_id: str | None,
    target_commit: str | None = None,
    reason: str | None = None,
    facts_provider: Callable[[], MergeFacts] | None = None,
) -> CrTransition:
    """唯一迁移入口。检查顺序照 `docs/12` §7.1：边 → 角色 → 理由 → 门禁 → STALE。

    顺序有实际意义：先判边/角色就不会为了一个必然被拒的操作去跑一次 daemon 门禁
    （门禁要读全库规则，贵且会产生 side effect 落库）。
    """
    target_status = next_status(status, event)
    assert_role(event, actor_id=actor_id, actor_role=actor_role, author_id=author_id)

    reason = (reason or "").strip() or None
    if event in REASON_REQUIRED_EVENTS and reason is None:
        raise CrError("REASON_REQUIRED", "驳回必须填写理由（E-05.1）", status=422)

    facts: MergeFacts | None = None
    if event == "merge":
        if facts_provider is None:
            raise CrError(
                "CANNOT_VERIFY", "合并需要门禁与 canonical 事实，未提供即拒绝（不许猜）",
                status=422,
            )
        facts = facts_provider()
        if facts.gate_has_error:
            raise CrError(
                "GATE_FAILED", "合并前置门禁未通过（E-05.2）", status=409,
                detail={"findings": list(facts.gate_findings),
                        "revert_to": GATE_FAILURE_REVERT},
            )
        if not target_commit:
            raise CrError(
                "MISSING_TARGET_COMMIT",
                "CR 未记录目标提交，无法确认要合入哪一版（E-05.3）", status=422,
            )
        if not facts.canonical_head:
            raise CrError(
                "CANNOT_VERIFY", "读不到 canonical HEAD，拒绝在无法比对的情况下合并",
                status=422,
            )
        if target_commit != facts.canonical_head:
            raise CrError(
                "STALE", "canonical 已前进，请刷新后重新提交（E-05.3）", status=409,
                detail={"target_commit": target_commit,
                        "canonical_head": facts.canonical_head,
                        "comment": _stale_comment(target_commit, facts.canonical_head)},
            )

    return CrTransition(
        event=event, from_status=status, to_status=target_status,
        actor_id=actor_id, reason=reason, facts=facts,
    )


def failure_comment(err: CrError) -> str | None:
    """把失败翻成系统评论（只对需要留痕的失败给文本）。

    门禁失败与 STALE 必须留痕：用户在评论里能看到「为什么没合上」，
    而不是只有一个 409 状态码。
    """
    if err.code == "GATE_FAILED":
        return _gate_comment(tuple(err.detail.get("findings") or ()))
    if err.code == "STALE":
        return str(err.detail.get("comment") or "")
    return None


def system_close_comment(reason: str) -> str:
    return f"系统：CR 自动关闭 —— {reason}（E-05.6）"


# ---------------------------------------------------------------- 落库编排


class CrService:
    """迁移的副作用层：写状态 + 系统评论 + 审计。

    判定不在这里（见 `transition()`）；本类只保证「判定通过后三件事一起发生」，
    避免路由里出现「改了状态忘了写审计」这种半截操作。
    """

    def __init__(self, repo) -> None:
        self._repo = repo

    def apply(
        self, cr, plan: CrTransition, *, by: str | None,
        comments: tuple[str, ...] = (),
    ):
        """落一次迁移。状态写入用 `from_statuses` 做乐观并发：并发者失败而非覆盖。"""
        updated = self._repo.set_cr_status(
            cr.id, plan.to_status, plan.actor_id,
            from_statuses=(plan.from_status,),
        )
        if updated is None:
            raise CrError("CR_NOT_FOUND", "CR 不存在", status=404)

        if plan.reason:
            self._repo.add_cr_comment(cr.id, plan.actor_id, plan.reason)
        for body in comments:
            if body.strip():
                self._repo.add_cr_comment(cr.id, None, body)

        self._repo.record_sync(
            action=_AUDIT_ACTION.get(plan.event, "resolve"),
            skill_id=cr.skill_id, runtime_id=None,
            detail={
                "cr_id": cr.id, "event": plan.event,
                "from": plan.from_status, "to": plan.to_status,
                "target_commit": cr.target_commit,
            },
            by=by,
        )
        return updated

    def revert_after_gate_failure(self, cr, *, by: str | None, comment: str) -> None:
        """E-05.2：门禁失败 → 退回 open + 系统评论（唯一允许的回跳）。"""
        self._repo.set_cr_status(
            cr.id, GATE_FAILURE_REVERT, None, from_statuses=("approved",),
        )
        self._repo.add_cr_comment(cr.id, None, comment)
        self._repo.record_sync(
            action="resolve", skill_id=cr.skill_id, runtime_id=None,
            detail={"cr_id": cr.id, "event": "merge_blocked_by_gate",
                    "from": "approved", "to": GATE_FAILURE_REVERT},
            by=by,
        )

    def close_for_deleted_skill(
        self, skill_id: str, *, by: str | None = None,
        reason: str = "目标 Skill 已删除或移出团队",
    ) -> list[str]:
        """E-05.6：目标 Skill 不在了，未终结的 CR 批量关闭 + 系统评论。

        返回被关闭的 CR id 列表（调用方据此发 `cr.updated`）。
        """
        closed: list[str] = []
        for cr in self._repo.list_skill_crs(skill_id):
            if cr.status in TERMINAL_STATUSES:
                continue
            self._repo.set_cr_status(
                cr.id, "closed", None, from_statuses=("open", "approved"),
            )
            self._repo.add_cr_comment(
                cr.id, None, system_close_comment(reason),
            )
            self._repo.record_sync(
                action="resolve", skill_id=skill_id, runtime_id=None,
                detail={"cr_id": cr.id, "event": "auto_close",
                        "from": cr.status, "to": "closed", "reason": reason},
                by=by,
            )
            closed.append(cr.id)
        return closed


_AUDIT_ACTION: dict[str, str] = {
    "approve": "resolve",
    "reject": "resolve",
    "close": "resolve",
    "merge": "push",   # 合并 = 写操作，沿用 push 口径（NFR-9 可查）
}
