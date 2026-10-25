"""上游检查（FR-1.0.13 / FR-2.2.10 / E-08）。

上游事实唯一权威在 `upstream_sources` 表（`core/fs/upstream.py` 只做判定，
本层负责取证与失败计数）。

三条口径（E-08）：

- **参与范围**：只查有 `upstream_sources` 记录的 skill；`type=local` 恒 `current`；
  **没有记录的 skill 上游维度整体 unknown**（E-08.2）—— 不按"没记录 = 最新"猜；
- **divergence 触发**：`outdated` → decay `degraded`；`error/unknown` → decay `unknown`
  （E-08.3，映射在 `core/algorithm/decay.py`）；
- **失败处理**：连续 3 次网络失败才置 `error`，下个周期重试（E-08.4）。
  计数落在 `upstream_sources.consecutive_failures`，单次抖动不改状态。

锁边界（T17）：`ls-remote` 是网络 IO，**永不在写锁内**。本服务自己不加锁 ——
调用方（daemon 日任务 / 手动 op）也不该把它包进 `write_lock`。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from skillos.core.fs.upstream import UpstreamSource as ProbeSource
from skillos.core.fs.upstream import UpstreamStatusKind, decide_status
from skillos.core.fs.version import GitError
from skillos.core.logging import get_logger
from skillos.store.index_repo import SkillQuery, UpstreamSourceRecord

log = get_logger(__name__)

# E-08.4：连续失败阈值。三次是为了跨过"代理抖动 / DNS 抽风"级别的一次性故障。
FAILURE_THRESHOLD = 3


@dataclass(frozen=True)
class CheckOutcome:
    slug: str
    status: str           # current / outdated / error / unknown / skipped
    detail: str | None = None
    upstream_hash: str | None = None

    def to_dict(self) -> dict:
        return {"slug": self.slug, "status": self.status,
                "detail": self.detail, "upstream_hash": self.upstream_hash}


class UpstreamService:
    """单 skill / 全量上游检查。需要 index store 与 git（取远端 ref）。"""

    def __init__(self, db, git, *, probe=None, sleep=time.sleep) -> None:
        self._db = db
        self._git = git
        # probe 注入点：测试用它替换网络 IO（默认走 GitRepo.remote_head）
        self._probe = probe
        self._sleep = sleep

    # ---------------------------------------------------------------- 取数

    def _probe_remote(self, url: str, ref: str | None) -> str | None:
        if self._probe is not None:
            return self._probe(url, ref)
        return self._git.remote_head(url, ref)

    def status_of_skill(self, skill_id: str) -> str | None:
        """decay 维度吃这个值：None = 没有上游记录（→ unknown）。"""
        row = self._db.get_upstream_source(skill_id)
        return None if row is None else row.status

    # ---------------------------------------------------------------- 判定

    def check(self, slug: str) -> CheckOutcome:
        """检查单个 skill。任何"判断不了"都回 unknown，不抛异常。"""
        record = self._db.get_skill_by_slug(slug)
        if record is None:
            return CheckOutcome(slug, "skipped", "未纳管")
        source = self._db.get_upstream_source(record.id)
        if source is None:
            # E-08.2：没记录 = 这个 skill 不参与上游跟踪，维度整体 unknown
            return CheckOutcome(slug, "unknown", "无 upstream_sources 记录")

        probe = ProbeSource(
            skill_id=source.skill_id, type=source.type, url=source.url,
            ref=source.ref, base_version=source.base_version,
            upstream_hash=source.upstream_hash,
        )
        if (source.type or "").lower() == "local":
            decided = decide_status(probe, None)
            self._persist(source, decided.status, source.upstream_hash, failures=0)
            return CheckOutcome(slug, decided.status, decided.detail,
                                source.upstream_hash)

        try:
            remote_hash = self._probe_remote(source.url or "", source.ref)
        except GitError as e:
            return self._on_failure(slug, source, str(e))

        decided = decide_status(probe, remote_hash)
        # 首次成功比对（无 baseline）只记录 hash，不把 unknown 当 outdated 报
        new_hash = decided.upstream_hash or source.upstream_hash
        self._persist(source, str(decided.status), new_hash, failures=0)
        log.info("upstream_checked", skill_slug=slug, status=str(decided.status))
        return CheckOutcome(slug, str(decided.status), decided.detail, new_hash)

    def _on_failure(self, slug: str, source: UpstreamSourceRecord,
                    detail: str) -> CheckOutcome:
        """E-08.4：连续失败满 3 次才置 error，其余情况保留原状态。"""
        failures = source.consecutive_failures + 1
        status = "error" if failures >= FAILURE_THRESHOLD else source.status
        self._persist(source, status, source.upstream_hash, failures=failures)
        log.warning(
            "upstream_check_failed", skill_slug=slug, failures=failures,
            status=status, detail=detail[:200],
        )
        return CheckOutcome(slug, status, detail, source.upstream_hash)

    def _persist(self, source: UpstreamSourceRecord, status: str,
                 upstream_hash: str | None, *, failures: int) -> None:
        self._db.upsert_upstream_source(UpstreamSourceRecord(
            skill_id=source.skill_id, type=source.type, url=source.url,
            ref=source.ref, base_version=source.base_version,
            upstream_hash=upstream_hash, last_checked_at=datetime.now(UTC),
            status=status, consecutive_failures=failures,
        ))

    # ---------------------------------------------------------------- 批量

    def check_all(self) -> dict:
        """全量检查（daemon 每日任务 / Web「立即检查」共用）。

        只遍历**有上游记录**的 skill（E-08.2），没有记录的连网络都不碰。
        """
        outcomes: list[CheckOutcome] = []
        sources = {s.skill_id: s for s in self._db.list_upstream_sources()}
        if not sources:
            return {"checked": 0, "items": [], "summary": {}}
        for record in self._db.list_skills(SkillQuery(limit=10000)):
            if record.id not in sources:
                continue
            outcomes.append(self.check(record.slug))
        summary: dict[str, int] = {}
        for o in outcomes:
            summary[o.status] = summary.get(o.status, 0) + 1
        log.info("upstream_check_all_completed", checked=len(outcomes), summary=summary)
        return {
            "checked": len(outcomes),
            "items": [o.to_dict() for o in outcomes],
            "summary": summary,
            "checked_at": datetime.now(UTC).isoformat(),
        }


def is_known(status: str | None) -> bool:
    """`unknown` 与 `error` 都不算"已知健康"（E-08.3：UI 显示「未知」）。"""
    return status in (UpstreamStatusKind.CURRENT.value, UpstreamStatusKind.OUTDATED.value)
