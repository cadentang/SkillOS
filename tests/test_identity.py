"""稳定身份分层匹配单测（04 §2.1.5 / FR-1.0.21 / 07 N8）。

坑（07 N8，变异验证）：上游 1.40.0 真实事故 —— Code Review 与 code-review
并存时按相似度/slug 任意挑一个，「导回仓库」覆盖掉另一个 Skill。
任一层多候选必须"不认"，禁止自行挑一个。
"""

from __future__ import annotations

from skillos.core.algorithm.identity import (
    IdentityCandidate,
    IdentityResolution,
    resolve_identity,
)
from skillos.core.model import SkillMeta


def _meta(name: str = "demo") -> SkillMeta:
    return SkillMeta(name=name, description="d")


def _cand(skill_id: str, source_ref: str | None = None,
          content_hash: str | None = None, dir_name: str | None = None) -> IdentityCandidate:
    return IdentityCandidate(
        skill_id=skill_id, source_ref=source_ref, content_hash=content_hash, dir_name=dir_name
    )


class TestLayer1SourceRef:
    def test_unique_source_ref_match(self) -> None:
        existing = [_cand("s1", source_ref="github:foo/bar@abc"), _cand("s2", source_ref="other")]
        res = resolve_identity(
            _meta(), content_hash="h1", source_ref="github:foo/bar@abc", existing=existing
        )
        assert res.status == "matched"
        assert res.identity is not None
        assert res.identity.skill_id == "s1"
        assert res.identity.matched_by == "source_ref"
        assert res.identity.confidence == "certain"

    def test_multiple_source_ref_candidates_ambiguous(self) -> None:
        existing = [
            _cand("s1", source_ref="x"),
            _cand("s2", source_ref="x"),
        ]
        res = resolve_identity(_meta(), "h1", "x", existing)
        assert res.status == "ambiguous"
        assert res.identity is None


class TestLayer2ContentHash:
    def test_unique_content_hash_match(self) -> None:
        existing = [_cand("s1", content_hash="hash-a"), _cand("s2", content_hash="hash-b")]
        res = resolve_identity(_meta(), "hash-a", source_ref=None, existing=existing)
        assert res.status == "matched"
        assert res.identity is not None
        assert res.identity.matched_by == "content_hash"
        assert res.identity.confidence == "probable"

    def test_multiple_hash_candidates_ambiguous(self) -> None:
        existing = [_cand("s1", content_hash="dup"), _cand("s2", content_hash="dup")]
        res = resolve_identity(_meta(), "dup", None, existing)
        assert res.status == "ambiguous"


class TestLayer3DirName:
    def test_unique_dir_name_casefold_match(self) -> None:
        existing = [_cand("s1", dir_name="Code-Review")]
        res = resolve_identity(_meta(), "h", None, existing, dir_name="code-review")
        assert res.status == "matched"
        assert res.identity is not None
        assert res.identity.matched_by == "dir_name"

    def test_code_review_collision_ambiguous(self) -> None:
        """07 N8：Code-Review 与 code-review 归一化后同键并存 → 不认，禁止挑一个。"""
        existing = [_cand("s1", dir_name="Code-Review"), _cand("s2", dir_name="code-review")]
        res = resolve_identity(_meta(), "h", None, existing, dir_name="code-review")
        assert res.status == "ambiguous"
        assert res.identity is None
        assert len(res.candidates) == 2

    def test_nfc_folds_chinese_dir_name(self) -> None:
        composed = "café"
        decomposed = "cafe\u0301"
        existing = [_cand("s1", dir_name=composed)]
        res = resolve_identity(_meta(), "h", None, existing, dir_name=decomposed)
        assert res.status == "matched"


class TestNoMatch:
    def test_no_candidates_new(self) -> None:
        res = resolve_identity(_meta(), "h1", None, [])
        assert res.status == "new"
        assert res.identity is None

    def test_no_layer_match_new(self) -> None:
        existing = [_cand("s1", source_ref="a", content_hash="x", dir_name="other")]
        res = resolve_identity(_meta(), "h1", None, existing, dir_name="fresh")
        assert res.status == "new"


class TestLayerPriority:
    def test_source_ref_beats_hash(self) -> None:
        """层级顺序即优先级：source_ref 命中就不再往下猜。"""
        existing = [_cand("s1", source_ref="r", content_hash="other")]
        res = resolve_identity(_meta(), "h", "r", existing)
        assert res.identity is not None
        assert res.identity.matched_by == "source_ref"


def test_resolution_type() -> None:
    """返回结构稳定（IdentityResolution），供 import 决策表消费。"""
    res = IdentityResolution(status="new", identity=None, candidates=[])
    assert res.status == "new"
