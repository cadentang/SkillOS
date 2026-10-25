"""相似检测编排（E-04 / N-07，`docs/12` §6.3）。

分层：算法与推荐链在 `core/algorithm/similarity.py`（纯函数），本层只做
**取正文 → 比对 → 覆盖式落库 → 组装建议卡读数**。

三个口径点：

- **触发独立于 check 性能基准**（TQ-03）：相似比对不在 push 前置路径上，
  由 check 尾部 / 详情页「立即比对」手动触发 —— 它读全库正文，不该拖慢 NFR-V2-01；
- **幂等**（E-04 GWT ③）：同输入不新增行（`replace_similarity` 按键覆盖 + 清理过期对）；
- **建议只影响默认选中项**（N-07.2）：推荐链给出 keep/drop 与理由，**永不自动执行**。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from skillos.core.algorithm.checks import summarize_health
from skillos.core.algorithm.similarity import (
    SIMILARITY_MERGE_THRESHOLD,
    SIMILARITY_TEXT_MAX_CHARS,
    SIMILARITY_THRESHOLD,
    SimilarPair,
    build_suggestion,
    find_similar,
)
from skillos.core.model import SkillSnapshot
from skillos.store.index_repo import SkillQuery


@dataclass(frozen=True)
class SimilarityRun:
    """一次比对的结果（给 CLI / op 回执用，不返回正文）。"""

    pairs: int
    added: int
    skills: int
    threshold: float


def text_of(snapshot: SkillSnapshot) -> str:
    """拼 name + description + 正文（E-04.1 的输入口径），按字符截断（E-04.4）。

    截断发生在**拼接之后**：先截 SKILL.md 再截正文会让短描述的小 skill
    在长文库里吃亏（词袋被稀释），分数不可比。
    """
    parts = [snapshot.meta.name or "", snapshot.meta.description or ""]
    for entry in snapshot.files:
        try:
            parts.append(entry.content.decode("utf-8"))
        except UnicodeDecodeError:
            continue
    return "\n".join(parts)[:SIMILARITY_TEXT_MAX_CHARS]


class SimilarityService:
    """比对与建议卡（只读落库缓存，不碰 canonical 内容）。"""

    def __init__(self, *, store, snapshots: Callable[[], dict[str, SkillSnapshot]]) -> None:
        self._store = store
        self._snapshots = snapshots

    # ---------------------------------------------------------------- 比对

    def run(self, *, threshold: float = SIMILARITY_THRESHOLD) -> SimilarityRun:
        snapshots = self._snapshots()
        # 键必须是 `skills.id`（不是快照自带的 id、更不是 slug）：
        # `skill_similarity` 两列都是指向 `skills.id` 的外键，用错键会静默落成死行
        texts: dict[str, str] = {}
        slug_of: dict[str, str] = {}
        for snapshot in snapshots.values():
            record = self._store.get_skill_by_slug(snapshot.slug)
            if record is None:
                continue        # canonical 有、索引没有：不猜，交给 scan 修
            texts[record.id] = text_of(snapshot)
            slug_of[record.id] = record.slug

        # 归一化对的方向（`skill_a_id < skill_b_id`，与 `ck_similarity_pair` 一致）：
        # `find_similar` 的输出顺序取决于 dict/set 迭代顺序，跨进程不稳定。方向一变，
        # (a,b) 就与库里旧行不相等 → 覆盖式落库会先删后插，"幂等不新增行"（E-04 GWT ③）
        # 随之破掉；方向反了还会直接撞 CHECK 约束。
        pairs: list[SimilarPair] = []
        for pair in find_similar(texts, threshold=threshold):
            a, b = pair.skill_a_id, pair.skill_b_id
            if a > b:
                a, b = b, a
            pairs.append(SimilarPair(a, b, pair.score, pair.method))
        pairs.sort(key=lambda p: (slug_of.get(p.skill_a_id, ""), slug_of.get(p.skill_b_id, "")))
        added = self._store.replace_similarity([
            (p.skill_a_id, p.skill_b_id, p.score, p.method) for p in pairs
        ])
        return SimilarityRun(pairs=len(pairs), added=added,
                             skills=len(texts), threshold=threshold)

    # ---------------------------------------------------------------- 展示

    def list_pairs(self, *, threshold: float = SIMILARITY_THRESHOLD) -> list[dict]:
        """相似对 + 建议（N-07）：只给读数与推荐，不给任何自动动作。"""
        rows = self._store.list_similarity(threshold=threshold)
        if not rows:
            return []
        usage = self._usage_90d()
        health = self._health_by_skill()
        created = {
            record.id: record.created_at.isoformat()
            for record in self._store.list_skills(SkillQuery(limit=10000))
            if record.created_at is not None
        }

        out: list[dict] = []
        for slug_a, slug_b, id_a, id_b, score in rows:
            suggestion = build_suggestion(
                SimilarPair(id_a, id_b, score),
                slug_a=slug_a, slug_b=slug_b,
                usage_90d=usage, health=health, created_at=created,
            )
            out.append({
                "a": slug_a,
                "b": slug_b,
                "score": score,
                "method": "tfidf",
                "merge_candidate": score >= SIMILARITY_MERGE_THRESHOLD,
                "suggestion": (
                    {"keep": suggestion.keep, "drop": suggestion.drop,
                     "reason": suggestion.reason}
                    if suggestion else None
                ),
            })
        return out

    # ---------------------------------------------------------------- 读数

    def _usage_90d(self) -> dict[str, int]:
        """近 90 天调用次数（N-01 口径，按 skill_id 聚合）。"""
        since = (datetime.now(UTC) - timedelta(days=90)).date()
        totals: dict[str, int] = defaultdict(int)
        for row in self._store.list_usage_daily(since):
            totals[row.skill_id] += int(row.count or 0)
        return dict(totals)

    def _health_by_skill(self) -> dict[str, int]:
        """按落库的 `skill_checks` 还原健康分（与 Dashboard 同一算法，口径单点）。"""
        grouped: dict[str, list] = defaultdict(list)
        for row in self._store.list_checks():
            grouped[row.skill_id].append(row)

        scores: dict[str, int] = {}
        for skill_id, rows in grouped.items():
            findings = [{"level": row.level} for row in rows if row.scope == "gate"]
            decay: dict[str, str] = {}
            for row in rows:
                if row.scope != "decay":
                    continue
                # 落库时 ok→info / degraded→warn / unknown→unknown（E-01.3），此处还原
                decay[f"{row.rule}_status"] = {
                    "warn": "degraded", "unknown": "unknown",
                }.get(row.level, "ok")
            scores[skill_id] = summarize_health(findings, decay)["score"]
        return scores


def window_start(days: int = 90, *, today: date | None = None) -> date:
    """窗口起点（抽成函数便于测试固定时钟，不在业务里读 date.today()）。"""
    return (today or datetime.now(UTC).date()) - timedelta(days=days)
