"""相似检测（FR-1.0.12）：TF-IDF 余弦，纯函数。"""

from __future__ import annotations

from skillos.core.algorithm.similarity import find_similar


def test_near_duplicate_detected():
    texts = {
        "a": "pdf report generator create pdf report from markdown",
        "b": "pdf report generator create pdf report from markdown text",
        "c": "docker compose lint yaml files",
    }
    pairs = find_similar(texts)
    assert len(pairs) == 1
    p = pairs[0]
    assert (p.skill_a_id, p.skill_b_id) == ("a", "b")  # a < b，与表约束一致
    assert p.score >= 0.75
    assert p.method == "tfidf"


def test_paraphrase_not_flagged():
    """改写（非逐字重复）不应误报 —— 阈值 0.75 的语义是「重复」不是「相关」。"""
    texts = {
        "a": "pdf-report-generator make pdf reports from markdown files",
        "b": "pdf-report-maker generate pdf reports from markdown sources",
        "c": "docker-compose-lint lint docker compose yaml",
    }
    assert find_similar(texts) == []


def test_ordering_and_edge_cases():
    assert find_similar({}) == []
    assert find_similar({"only": "one skill"}) == []
    # 排序断言（小语料 TF-IDF 分数偏低，用低阈值触发两对再验降序）
    texts = {
        "x": "alpha beta gamma",
        "y": "alpha beta gamma",
        "z": "alpha beta gamma delta",
        "w": "omega psi",
    }
    pairs = find_similar(texts, threshold=0.3)
    assert len(pairs) >= 2  # x-y 必在；x-z / y-z 同分各为一对
    scores = [p.score for p in pairs]
    assert scores == sorted(scores, reverse=True)


def test_cjk_tokens():
    """中文逐字分词：高度重复的中文文本可检出。"""
    texts = {
        "a": "代码审查助手 审查代码质量",
        "b": "代码审查助手 审查代码质量",
        "c": "数据库迁移工具",
    }
    pairs = find_similar(texts)
    assert any(p.skill_a_id == "a" and p.skill_b_id == "b" and p.score > 0.9
               for p in pairs)
