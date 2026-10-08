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
        # tf-idf：tf * log((n+1)/(df+1))（平滑，避免 df=n 时权重归零）
        return {
            t: cnt * math.log((n + 1) / (df[t] + 1))
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
