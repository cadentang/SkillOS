"""质量门禁单测（04 §2.1.4 / FR-1.0.11 / NFR-10.5）。

NFR-10.5：check 命令与 push 前置校验调用同一函数（run_gate 唯一入口）——
两处判定漂移会导致"CLI 通过而 push 拦截"，用户将不再信任门禁。
demo 高潮一（删 description → push 被拦）依赖本模块的 error 判定。
"""

from __future__ import annotations

from skillos.core.algorithm.checks import (
    CheckLevel,
    GateContext,
    RuleFinding,
    estimate_tokens,
    run_gate,
    summarize_health,
)
from skillos.core.model import FileEntry, SkillMeta, SkillSnapshot


def _snapshot(
    meta: SkillMeta | None,
    files: tuple[FileEntry, ...] | None = None,
    body: str = "正文\n",
) -> SkillSnapshot:
    content = (
        b"---\nname: t\n---\n" + body.encode("utf-8")
        if meta is None
        else b"---\n" + _fm_bytes(meta) + b"---\n" + body.encode("utf-8")
    )
    all_files = files if files is not None else (FileEntry("SKILL.md", content),)
    if all(f.path != "SKILL.md" for f in all_files):
        all_files = (FileEntry("SKILL.md", content), *all_files)
    return SkillSnapshot(
        skill_id="t", slug="t",
        meta=meta or SkillMeta(name="", description=""),
        files=all_files,
    )


def _fm_bytes(meta: SkillMeta) -> bytes:
    lines = [f"name: {meta.name}", f"description: {meta.description}"]
    if meta.version:
        lines.append(f"version: {meta.version}")
    for k, v in (meta.extra or {}).items():
        lines.append(f"{k}: {v}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _ctx(**kw) -> GateContext:
    defaults = dict(known_slugs=frozenset(), dependencies=(), dependency_graph={})
    defaults.update(kw)
    return GateContext(**defaults)


def _rules(res) -> set[str]:
    return {f.rule for f in res.findings}


class TestErrorRules:
    """error 级必须拦 push（FR-1.0.11，不可退让项）。"""

    def test_missing_name_and_description(self) -> None:
        res = run_gate(_snapshot(meta=None), _ctx())
        assert "fm.name.required" in _rules(res)
        assert "fm.description.required" in _rules(res)
        assert res.has_error is True

    def test_missing_skill_md(self) -> None:
        snap = SkillSnapshot(
            skill_id="t", slug="t",
            meta=SkillMeta(name="t", description="d" * 40),
            files=(FileEntry("other.md", b"x"),),
        )
        res = run_gate(snap, _ctx())
        assert "struct.skill_md" in _rules(res)
        assert res.has_error is True

    def test_dependency_missing(self) -> None:
        meta = SkillMeta(name="t", description="d" * 40)
        ctx = _ctx(known_slugs=frozenset({"other"}), dependencies=["gone"])
        res = run_gate(_snapshot(meta), ctx)
        assert "dep.missing" in _rules(res)
        assert res.has_error is True


class TestWarnRules:
    def test_description_too_short(self) -> None:
        meta = SkillMeta(name="t", description="短")
        res = run_gate(_snapshot(meta), _ctx())
        assert "fm.description.length" in _rules(res)
        assert res.has_error is False  # warn 不拦截

    def test_description_too_long(self) -> None:
        meta = SkillMeta(name="t", description="x" * 501)
        assert "fm.description.length" in _rules(run_gate(_snapshot(meta), _ctx()))

    def test_name_not_kebab(self) -> None:
        meta = SkillMeta(name="Code Review", description="d" * 40)
        res = run_gate(_snapshot(meta), _ctx())
        assert "fm.name.kebab" in _rules(res)
        assert res.has_error is False

    def test_asset_untracked(self) -> None:
        """FR-1.0.20：含超阈值未纳管资产 → warn。"""
        meta = SkillMeta(name="t", description="d" * 40)
        files = (FileEntry("SKILL.md", b"x"), FileEntry("big.bin", b"", in_git=False))
        snap = SkillSnapshot("t", "t", meta, files)
        assert "asset.untracked" in _rules(run_gate(snap, _ctx()))

    def test_dependency_cycle(self) -> None:
        meta = SkillMeta(name="t", description="d" * 40)
        graph = {"t": ["u"], "u": ["t"]}
        res = run_gate(
            _snapshot(meta),
            _ctx(known_slugs=frozenset({"u"}), dependency_graph=graph, dependencies=["u"]),
        )
        assert "dep.cycle" in _rules(res)
        assert res.has_error is False

    def test_struct_ref_missing(self) -> None:
        meta = SkillMeta(name="t", description="d" * 40)
        snap = _snapshot(meta, body="看 [参考](references/gone.md)\n")
        assert "struct.ref.missing" in _rules(run_gate(snap, _ctx()))


class TestInfoRules:
    def test_tokens_hint(self) -> None:
        long_desc = "很长的描述" * 60  # >150 tokens
        meta = SkillMeta(name="t", description=long_desc)
        assert "fm.description.tokens" in _rules(run_gate(_snapshot(meta), _ctx()))

    def test_unknown_fields_listed(self) -> None:
        meta = SkillMeta(name="t", description="d" * 40, extra={"mystery": "v"})
        res = run_gate(_snapshot(meta), _ctx())
        assert "fm.unknown" in _rules(res)
        finding = next(f for f in res.findings if f.rule == "fm.unknown")
        assert "mystery" in finding.message


class TestCleanSkill:
    def test_no_findings(self) -> None:
        meta = SkillMeta(name="t", description="a" * 40)
        snap = _snapshot(
            meta,
            files=(FileEntry("SKILL.md", b"x"), FileEntry("references/a.md", b"y")),
            body="看 [参考](references/a.md)\n",
        )
        res = run_gate(snap, _ctx())
        assert res.findings == []
        assert res.has_error is False


def test_estimate_tokens() -> None:
    assert estimate_tokens("四个中文字") == 5
    assert estimate_tokens("abcdefgh") == 2  # 8 英文字符 ≈ 2 token


def test_finding_levels_are_typed() -> None:
    f = RuleFinding(rule="x", level=CheckLevel.WARN, message="m")
    assert f.level == "warn"


class TestSummarizeHealth:
    """健康度汇总（FR-1.0.24 退化形态：门禁结果计数，砍单第 8 项）。

    - T11 红线：health 只用于展示与排序，放行判定只有 GateResult.has_error
      一个入口（NFR-10.5）——若把计数接入放行逻辑，治理叙事当场失效（04 §13.13）。
    - NFR-10.3：必须可下钻——计数与 findings/decay 原始值一一对应。
    """

    def test_counts_mixed_levels(self) -> None:
        findings = [
            RuleFinding("a", CheckLevel.ERROR, "m"),
            RuleFinding("b", CheckLevel.WARN, "m"),
            RuleFinding("c", CheckLevel.WARN, "m"),
            RuleFinding("d", CheckLevel.INFO, "m"),
        ]
        h = summarize_health(findings)
        assert h["error_count"] == 1
        assert h["warn_count"] == 2
        assert h["info_count"] == 1

    def test_empty_findings_all_zero(self) -> None:
        h = summarize_health([])
        assert h["error_count"] == 0
        assert h["warn_count"] == 0
        assert h["info_count"] == 0
        assert h["staleness_days"] is None
        assert h["dependency_broken_count"] == 0
        # 无 decay 输入 => 三维全 unknown（不猜），但**不扣分**（E-01.3）
        assert h["score"] == 100
        assert h["degraded_decay_dimensions"] == 0
        assert h["unknown_dimensions"] == 3

    def test_accepts_dict_form(self) -> None:
        """daemon / API 路径拿到的是序列化 dict，两种形态必须同一口径。"""
        findings = [
            {"rule": "a", "level": "error", "message": "m"},
            {"rule": "b", "level": "warn", "message": "m"},
        ]
        h = summarize_health(findings)
        assert h["error_count"] == 1 and h["warn_count"] == 1

    def test_decay_passthrough(self) -> None:
        decay = {"slug": "t", "staleness_days": 12.5, "dependency_broken": ["x", "y"]}
        h = summarize_health([], decay)
        assert h["staleness_days"] == 12.5
        assert h["dependency_broken_count"] == 2

    def test_decay_missing_fields_do_not_guess(self) -> None:
        """折旧字段缺失 → None/0，不得用 updated_at 之类的 DB 列兜底（T16 同源红线）。"""
        h = summarize_health([], {"slug": "t"})
        assert h["staleness_days"] is None
        assert h["dependency_broken_count"] == 0

    def test_no_gate_flag_in_health(self) -> None:
        """T11 变异验证：health 结构里不得出现任何可当放行依据的位（has_error 等）。

        若未来有人把 has_error 塞进来"顺手"用于放行，删掉 GateResult 单一入口
        的约束就被架空——本测试必须变红。
        """
        h = summarize_health([RuleFinding("a", CheckLevel.ERROR, "m")])
        assert "has_error" not in h
        assert "pass" not in h
        assert "ok" not in h
