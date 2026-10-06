"""相似 / 重复检测（04 §2.1.5，FR-1.0.12）。

第一版：name + description + 正文词袋 → TF-IDF 余弦相似度，阈值 0.75 提示。
不引入 embedding —— 成本高、收益低，且相似判断最终由人确认。
纯函数：输入文本集合，输出相似对。
"""

from __future__ import annotations

from dataclasses import dataclass

SIMILARITY_THRESHOLD = 0.75


@dataclass(frozen=True)
class SimilarPair:
    skill_a_id: str
    skill_b_id: str
    score: float  # 0–1
    method: str = "tfidf"


def find_similar(texts: dict[str, str], threshold: float = SIMILARITY_THRESHOLD) -> list[SimilarPair]:
    """词袋 TF-IDF 余弦相似度。texts: skill_id → 拼接文本。"""
    raise NotImplementedError("P1 实现：TF-IDF 余弦，纯函数，不引入 embedding")
