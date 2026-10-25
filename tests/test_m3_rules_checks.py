"""M3 集成口径（N-03 / N-08 / E-04 / N-07）：规则集落位、安全扫描、白名单、相似对。

这四块的共同失败方式是**安静地无效**：规则没被拷贝、hash 没变、ignore 放行了真泄露、
相似对每次重跑都新增行。所以断言都落在"是否真的生效/是否真的幂等"上。
"""

from __future__ import annotations

from pathlib import Path

from skillos.core.algorithm.similarity import (
    SIMILARITY_MERGE_THRESHOLD,
    build_suggestion,
    find_similar,
    recommend_keep,
)
from skillos.services.engine import EnvPaths, LocalEngine
from skillos.services.rule_service import RuleService


def _skill_md(name: str, body: str, description: str | None = None) -> str:
    desc = description or f"A demo skill named {name} used by M3 tests only."
    return f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n"


def _engine(tmp_path: Path, skills: dict[str, str]) -> LocalEngine:
    home = tmp_path / "home"
    for slug, content in skills.items():
        d = home / ".claude" / "skills" / slug
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(content, encoding="utf-8")
    engine = LocalEngine(
        EnvPaths(repo_root=tmp_path / "repo", db_path=tmp_path / "state" / "s.db"),
        home=home,
    )
    engine.init_env()
    engine.import_skills()
    return engine


class TestRuleService:
    def test_builtin_copied_once_and_never_overwritten(self, tmp_path: Path) -> None:
        """N-08.1：首启拷贝；用户改过之后升级包不得覆盖。"""
        repo = tmp_path / "repo"
        repo.mkdir()
        service = RuleService(repo)
        assert service.ensure_builtin() == ["copied security.yaml"]

        target = repo / ".skillos" / "rules" / "security.yaml"
        edited = target.read_text(encoding="utf-8").replace("level: error", "level: warn")
        target.write_text(edited, encoding="utf-8")

        assert service.ensure_builtin() == []
        assert "level: warn" in target.read_text(encoding="utf-8")

    def test_ruleset_hash_tracks_bytes(self, tmp_path: Path) -> None:
        """N-03.5：改规则 → hash 变；同内容两次 → 不变（不看 mtime）。"""
        repo = tmp_path / "repo"
        rules = repo / ".skillos" / "rules"
        rules.mkdir(parents=True)
        (rules / "a.yaml").write_text(
            "rules:\n  - rule_id: custom.x\n    level: warn\n    target: description\n"
            "    match: {pattern: \"x\"}\n    message: m\n",
            encoding="utf-8",
        )
        service = RuleService(repo)
        first = service.ruleset_hash()
        assert first == RuleService(repo).ruleset_hash()

        (rules / "a.yaml").write_text(
            (rules / "a.yaml").read_text(encoding="utf-8") + "\n# 改动\n", encoding="utf-8"
        )
        assert service.ruleset_hash() == first      # 缓存命中（同指纹）
        service.invalidate()
        assert RuleService(repo).ruleset_hash() != first

    def test_ignore_entries_require_reason(self, tmp_path: Path) -> None:
        """没有理由的白名单等于后门：解析阶段直接不收。"""
        repo = tmp_path / "repo"
        rules = repo / ".skillos" / "rules"
        rules.mkdir(parents=True)
        (rules / "ignore.yaml").write_text(
            "ignores:\n"
            "  - rule_id: security.github-token\n    paths: ['docs/*.md']\n    reason: 文档示例\n"
            "  - rule_id: security.github-token\n    paths: ['*']\n",
            encoding="utf-8",
        )
        service = RuleService(repo)
        entries = service.ignores()
        assert len(entries) == 1 and entries[0].reason == "文档示例"
        assert service.ignored("security.github-token", "docs/a.md") is not None
        assert service.ignored("security.github-token", "SKILL.md") is None


class TestSecurityCheck:
    def test_token_hit_blocks_and_persists_ruleset_hash(self, tmp_path: Path) -> None:
        """N-08 GWT①：ghp_ token → error finding，push 会被拦（has_error）。"""
        engine = _engine(tmp_path, {
            "alpha": _skill_md("alpha", "token = ghp_" + "a" * 36),
        })
        report = engine.checks.check("alpha")
        assert report.gate.has_error
        assert any(f.rule == "security.github-token" for f in report.gate.findings)

        rows = [r for r in engine.db.list_checks("gate")]
        assert rows and rows[0].ruleset_hash == engine.rules.ruleset_hash()

    def test_ignore_suppresses_finding_but_leaves_audit_row(self, tmp_path: Path) -> None:
        """N-08 GWT②：命中 ignore → 无 finding，但留 info 行可审计。"""
        engine = _engine(tmp_path, {
            "alpha": _skill_md("alpha", "token = ghp_" + "b" * 36),
        })
        ignore = engine.paths.repo_root / ".skillos" / "rules" / "ignore.yaml"
        ignore.write_text(
            "ignores:\n  - rule_id: security.github-token\n"
            "    paths: ['SKILL.md']\n    reason: 测试样例\n",
            encoding="utf-8",
        )
        engine.rules.invalidate()   # 规则缓存失效后，同一个 RuleService 会重新解析
        report = engine.checks.check("alpha")

        assert not any(f.rule == "security.github-token" for f in report.gate.findings)
        audit = [r for r in engine.db.list_checks("gate") if "白名单" in r.message]
        assert audit and audit[0].level == "info"

    def test_untracked_large_file_is_still_scanned(self, tmp_path: Path, monkeypatch) -> None:
        """N-08.3 显式例外：`in_git=false` 的超阈值文件仍参与安全扫描。"""
        from skillos.core import constants

        monkeypatch.setattr(constants, "ASSET_MAX_BYTES", 64)

        engine = _engine(tmp_path, {"alpha": _skill_md("alpha", "ok")})
        big = engine.paths.repo_root / "skills" / "alpha" / "assets" / "dump.bin"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_text("x" * 128 + " ghp_" + "c" * 36, encoding="utf-8")

        report = engine.checks.check("alpha", include_untracked=True)
        assert any(f.rule == "security.github-token" for f in report.gate.findings)

    def test_push_path_does_not_scan_disk(self, tmp_path: Path, monkeypatch) -> None:
        """push 前置（`include_untracked=False`）不读盘 —— 热路径必须快。"""
        from skillos.core import constants

        monkeypatch.setattr(constants, "ASSET_MAX_BYTES", 64)
        engine = _engine(tmp_path, {"alpha": _skill_md("alpha", "ok")})
        big = engine.paths.repo_root / "skills" / "alpha" / "dump.bin"
        big.write_text("x" * 128 + " ghp_" + "d" * 36, encoding="utf-8")

        assert not any(
            f.rule == "security.github-token"
            for f in engine.gate_of("alpha", include_untracked=False).findings
        )


class TestSimilarity:
    def test_pair_orientation_is_canonical_and_stable(self, tmp_path: Path) -> None:
        """对的方向必须由 `skills.id` 归一（`ck_similarity_pair`：a<b）。

        方向跟着 dict 迭代顺序走会出两个后果：撞 CHECK 约束、以及每轮重跑先删后插
        （"幂等不新增行"失效）。这里用"倒序插入"把不确定性放大。
        """
        body = "alpha beta gamma delta epsilon zeta"
        engine = _engine(tmp_path, {
            "zeta-one": _skill_md("dup", body, description="same description for both"),
            "alpha-two": _skill_md("dup", body, description="same description for both"),
        })
        first = engine.similarity.run()
        assert first.pairs == 1 and first.added == 1

        rows = engine.db.list_similarity()
        assert len(rows) == 1
        _slug_a, _slug_b, id_a, id_b, _score = rows[0]
        assert id_a < id_b, "落库方向必须按 skills.id 归一"

        second = engine.similarity.run()
        assert second.added == 0, "同一输入重跑不得新增行（方向不稳会先删后插）"
        assert len(engine.db.list_similarity()) == 1

    def test_run_is_idempotent_and_clears_stale_pairs(self, tmp_path: Path) -> None:
        """E-04 GWT③：同一库跑两次 → 不新增行。

        两个目录放**同名同正文**的 skill（真·重复），这样分数是 1.0、能落到
        N-07 的合并建议分支；名字不同会让唯一的区分词被 IDF 放大到 0.8 区间，
        只测到提示路径，测不到「建议」这一段。
        """
        body = "alpha beta gamma delta epsilon zeta"
        engine = _engine(tmp_path, {
            "one": _skill_md("dup", body, description="same description for both skills"),
            "two": _skill_md("dup", body, description="same description for both skills"),
        })
        first = engine.similarity.run()
        assert first.pairs >= 1 and first.added == first.pairs
        second = engine.similarity.run()
        assert second.pairs == first.pairs and second.added == 0

        pairs = engine.similarity.list_pairs()
        # 对的方向由 `skills.id` 归一（`ck_similarity_pair` 的 a<b），与 slug 字典序无关，
        # 所以这里按无序对比较 —— 断言方向等于 slug 顺序会随 uuid 随机失败
        assert {frozenset((p["a"], p["b"])) for p in pairs} == {frozenset({"one", "two"})}
        assert pairs[0]["suggestion"] is not None       # ≥0.9 才给建议
        assert pairs[0]["suggestion"]["keep"] in {"one", "two"}

    def test_low_similarity_not_persisted(self) -> None:
        pairs = find_similar({
            "a": "alpha beta gamma delta",
            "b": "unrelated words entirely different topic",
        })
        assert pairs == []

    def test_recommend_chain_order(self) -> None:
        """N-07.2：①调用数 ②健康度 ③创建时间，逐级兜底且确定性。"""
        keep, reason = recommend_keep(
            "a", "b", usage_90d={"a": 9, "b": 2}, health={}, created_at={},
        )
        assert keep == "a" and "调用" in reason

        keep, reason = recommend_keep(
            "a", "b", usage_90d={"a": 2, "b": 2}, health={"a": 90, "b": 40}, created_at={},
        )
        assert keep == "a" and "健康度" in reason

        keep, reason = recommend_keep(
            "a", "b", usage_90d={}, health={},
            created_at={"a": "2026-01-01T00:00:00", "b": "2026-05-01T00:00:00"},
        )
        assert keep == "a" and "创建时间" in reason

        keep, _ = recommend_keep("b", "a", usage_90d={}, health={}, created_at={})
        assert keep == "a"        # 全平局按字典序，确定性可测

    def test_suggestion_only_above_merge_threshold(self) -> None:
        from skillos.core.algorithm.similarity import SimilarPair

        below = build_suggestion(
            SimilarPair("1", "2", SIMILARITY_MERGE_THRESHOLD - 0.05),
            slug_a="a", slug_b="b", usage_90d={}, health={}, created_at={},
        )
        assert below is None

        above = build_suggestion(
            SimilarPair("1", "2", SIMILARITY_MERGE_THRESHOLD + 0.05),
            slug_a="a", slug_b="b", usage_90d={"a": 5}, health={}, created_at={},
        )
        assert above is not None and above.keep == "a" and above.drop == "b"
