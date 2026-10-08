"""溯源记录（FR-1.0.22 / 04 §2.2.3）。

数据源直接用 git log（Commit 列表），不另建表 —— 这是它成本低的原因。
回滚本身也写溯源（GitRepo.rollback 的安全快照 + 回滚 commit 都带
"skillos: ..." 前缀），否则账本会出现空档：谁把哪个版本退回去了无从追查。

约定：skillos 应用层 commit 的首行格式为 `skillos: <action> <detail>`；
非该前缀的 commit 视为用户/编辑器操作（action=edit），时间轴照常显示。
"""

from __future__ import annotations

from dataclasses import dataclass

from skillos.core.model import Commit

# action 词表（新增动作必须同步此处；与 04 §2.2.3 的具名操作对应）
_ACTIONS = ("import", "distribute", "edit", "rollback", "snapshot", "resolve")
_PREFIX = "skillos:"


@dataclass(frozen=True)
class ProvenanceEvent:
    """溯源时间轴单元（FR-1.1.10 `skillos log` 与 Web 时间轴共用）。"""

    author: str
    commit: str
    at: str
    action: str  # import / distribute / edit / rollback / snapshot / resolve
    detail: str


def _classify(message: str) -> tuple[str, str]:
    """commit 首行 → (action, detail)。"""
    first_line = message.splitlines()[0] if message else ""
    stripped = first_line.strip()
    if stripped.startswith(_PREFIX):
        rest = stripped[len(_PREFIX):].strip()
        for action in _ACTIONS:
            if rest == action or rest.startswith(action + " "):
                return action, rest[len(action):].strip()
    return "edit", stripped


def provenance_events(commits: list[Commit]) -> list[ProvenanceEvent]:
    """git log（新→旧）→ 溯源事件（保持顺序，时间轴直接消费）。"""
    return [
        ProvenanceEvent(
            author=c.author,
            commit=c.sha,
            at=c.at,
            action=action,
            detail=detail,
        )
        for c in commits
        for action, detail in [_classify(c.message)]
    ]
