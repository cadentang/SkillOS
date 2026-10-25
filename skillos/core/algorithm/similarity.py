"""相似 / 重复检测（04 §2.1.5，FR-1.0.12）。

第一版：name + description + 正文词袋 → TF-IDF 余弦相似度，阈值 0.75 提示。
不引入 embedding —— 成本高、收益低，且相似判断最终由人确认。
纯函数：输入文本集合，输出相似对。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

SIMILARITY_THRESHOLD = 0.75
# N-07.1：≥0.9 才生成「合并建议」，0.75~0.9 维持纯提示
SIMILARITY_MERGE_THRESHOLD = 0.9
# E-04.4：单篇正文截断（≈5000 tokens；中文 1 字≈1 token，取 20000 字符留余量）
SIMILARITY_TEXT_MAX_CHARS = 20000

# 分词：CJK 逐字 + 西文/数字词（中英文混排的 skill 正文都能覆盖）
_WORD = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def _tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


@dataclass(frozen=True)
class SimilarPair:
    skill_a_id: str
    skill_b_id: str
    score: float  # 0–1
    method: str = "tfidf"


def find_similar(
    texts: dict[str, str], threshold: float = SIMILARITY_THRESHOLD
) -> list[SimilarPair]:
    """词袋 TF-IDF 余弦相似度。texts: skill_id → 拼接文本。

    复杂度 O(n²)（两两组合）：62 个真实 Skill 量级毫秒级完成，不建索引。
    结果按分数降序；skill_a_id < skill_b_id 与 skill_similarity 表约束一致。
    """
    ids = sorted(texts)
    if len(ids) < 2:
        return []
    counters = {i: Counter(_tokenize(texts[i])) for i in ids}
    df: Counter = Counter()
    for c in counters.values():
        df.update(c.keys())
    n = len(ids)

    def _vec(counter: Counter) -> dict[str, float]:
        # tf-idf：tf * (log((n+1)/(df+1)) + 1) —— 加一平滑。
        # 不平滑时 df=n 的词权重为 0：两篇**几乎相同**的 skill 全部分量归零，
        # 相似度反而是 0（62 个 skill 里最常见的场景直接失效）。
        return {
            t: cnt * (math.log((n + 1) / (df[t] + 1)) + 1.0)
            for t, cnt in counter.items()
        }

    vecs = {i: _vec(counters[i]) for i in ids}
    out: list[SimilarPair] = []
    for x in range(len(ids)):
        for y in range(x + 1, len(ids)):
            a, b = ids[x], ids[y]
            va, vb = vecs[a], vecs[b]
            if not va or not vb:
                continue
            dot = sum(w * vb.get(t, 0.0) for t, w in va.items())
            na = math.sqrt(sum(w * w for w in va.values()))
            nb = math.sqrt(sum(w * w for w in vb.values()))
            score = dot / (na * nb) if na and nb else 0.0
            if score >= threshold:
                out.append(SimilarPair(a, b, round(score, 4)))
    out.sort(key=lambda p: (-p.score, p.skill_a_id, p.skill_b_id))
    return out


# ---------------------------------------------------------------- 治理建议（N-07）

@dataclass(frozen=True)
class MergeSuggestion:
    """一条合并建议：保留谁、回收谁、为什么（原因必须可解释，NFR-10.3）。"""

    keep: str
    drop: str
    score: float
    reason: str


def recommend_keep(
    a: str,
    b: str,
    *,
    usage_90d: dict[str, int],
    health: dict[str, int],
    created_at: dict[str, str],
) -> tuple[str, str]:
    """N-07.2 推荐链：①近 90 天调用多 ②健康度高 ③更早创建。

    三级全平局时按 slug 字典序取小 —— 确定性是可测的前提，
    「随机选一个」会让"同一份数据两次建议不同"，用户没法复核。
    返回 `(keep, reason)`。
    """
    va, vb = usage_90d.get(a, 0), usage_90d.get(b, 0)
    if va != vb:
        keep = a if va > vb else b
        return keep, f"近 90 天调用更多（{max(va, vb)} vs {min(va, vb)}）"

    ha, hb = health.get(a, 0), health.get(b, 0)
    if ha != hb:
        keep = a if ha > hb else b
        return keep, f"健康度更高（{max(ha, hb)} vs {min(ha, hb)}）"

    ca, cb = created_at.get(a, ""), created_at.get(b, "")
    if ca != cb:
        keep = a if ca < cb else b
        return keep, "创建时间更早"

    return (a if a < b else b), "评分完全打平，按名称字典序取前者"


def build_suggestion(
    pair: SimilarPair,
    *,
    slug_a: str,
    slug_b: str,
    usage_90d: dict[str, int],
    health: dict[str, int],
    created_at: dict[str, str],
) -> MergeSuggestion | None:
    """仅 ≥ `SIMILARITY_MERGE_THRESHOLD` 才给建议（N-07.1：其余只是提示）。"""
    if pair.score < SIMILARITY_MERGE_THRESHOLD:
        return None
    keep, reason = recommend_keep(
        slug_a, slug_b, usage_90d=usage_90d, health=health, created_at=created_at,
    )
    return MergeSuggestion(
        keep=keep,
        drop=slug_b if keep == slug_a else slug_a,
        score=pair.score,
        reason=reason,
    )
