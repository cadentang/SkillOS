"""11 条内置门禁规则逐条验收（E-03.1 / E-03.2）。

- 每条规则至少一个用例（参数化），用例里断言 rule ID **和** level；
- `RULES` 必须与 `RuleId` 枚举一一对应（新增规则忘了登记就会红）；
- 覆盖性断言：11 条规则每条都至少被某个用例真正触发过 —— 防「有实现但永远打不到」。
- E-03.2 代码侧保障：`run_gate` 内不得出现规则 ID 字面量（只能引用枚举）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from skillos.core.algorithm.checks import (
    RULES,
    CheckLevel,
    GateContext,
    RuleId,
    run_gate,
)
from skillos.core.model import FileEntry, SkillMeta, SkillSnapshot

CHECKS_PY = (
    Path(__file__).resolve().parents[1] / "skillos" / "core" / "algorithm" / "checks.py"
)

_DESC_OK = "一个足够长的描述用于通过长度校验，说明这个 skill 的用途与输入输出边界。"


def _snap(
    *,
    name: str = "demo",
    description: str = _DESC_OK,
    body: str = "body",
    files: tuple[FileEntry, ...] | None = None,
    extra: dict | None = None,
    slug: str = "demo",
) -> SkillSnapshot:
    meta = SkillMeta(name=name, description=description, extra=extra or {})
    if files is None:
        files = (FileEntry(path="SKILL.md", content=body.encode()),)
    return SkillSnapshot(skill_id=slug, slug=slug, meta=meta, files=files)


CASES: list[tuple[RuleId, CheckLevel, SkillSnapshot, GateContext]] = [
    (
        RuleId.NAME_REQUIRED, CheckLevel.ERROR,
        _snap(name=""), GateContext(),
    ),
    (
        RuleId.DESCRIPTION_REQUIRED, CheckLevel.ERROR,
        _snap(description=""), GateContext(),
    ),
    (
        RuleId.DESCRIPTION_LENGTH, CheckLevel.WARN,
        _snap(description="太短"), GateContext(),
    ),
    (
        RuleId.DESCRIPTION_TOKENS, CheckLevel.INFO,
        _snap(description="字" * 200), GateContext(),
    ),
    (
        RuleId.NAME_KEBAB, CheckLevel.WARN,
        _snap(name="Bad_Name"), GateContext(),
    ),
    (
        RuleId.UNKNOWN_FIELD, CheckLevel.INFO,
        _snap(extra={"my_field": 1}), GateContext(),
    ),
    (
        RuleId.SKILL_MD_MISSING, CheckLevel.ERROR,
        _snap(files=(FileEntry(path="README.md", content=b"x"),)), GateContext(),
    ),
    (
        RuleId.REF_MISSING, CheckLevel.WARN,
        _snap(body="见 [guide](references/missing.md)"), GateContext(),
    ),
    (
        RuleId.DEP_MISSING, CheckLevel.ERROR,
        _snap(), GateContext(known_slugs=frozenset({"demo"}), dependencies=("ghost",)),
    ),
    (
        RuleId.DEP_CYCLE, CheckLevel.WARN,
        _snap(), GateContext(
            known_slugs=frozenset({"demo", "a"}),
            dependencies=("a",),
            dependency_graph={"demo": ["a"], "a": ["demo"]},
        ),
    ),
    (
        RuleId.ASSET_UNTRACKED, CheckLevel.WARN,
        _snap(files=(
            FileEntry(path="SKILL.md", content=b"body"),
            FileEntry(path="big.bin", content=b"0", is_text=False, in_git=False),
        )), GateContext(),
    ),
]


@pytest.mark.parametrize(("rule", "level", "snapshot", "ctx"), CASES,
                         ids=[c[0].value for c in CASES])
def test_rule_fires_with_expected_level(rule, level, snapshot, ctx) -> None:
    result = run_gate(snapshot, ctx)
    hits = [f for f in result.findings if f.rule == rule]
    assert hits, f"{rule} 未触发，实际 findings={[f.rule for f in result.findings]}"
    assert hits[0].level == level
    if level is CheckLevel.ERROR:
        assert result.has_error is True


class TestRuleIdContract:
    def test_rules_cover_exactly_the_eleven_ids(self) -> None:
        assert len(RuleId) == 11
        assert RULES == {item.value for item in RuleId}

    def test_every_rule_has_a_case(self) -> None:
        """E-03.1：11 条规则每条至少 1 个用例 —— 新增规则却忘了加用例就会红。"""
        covered = {case[0] for case in CASES}
        assert covered == set(RuleId)

    def test_no_rule_id_literal_in_run_gate(self) -> None:
        """E-03.2：规则 ID 是稳定契约，代码侧只准引用枚举，字面量会漏改。"""
        tree = ast.parse(CHECKS_PY.read_text(encoding="utf-8"))
        literals: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "RuleFinding" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                literals.append(f"line {node.lineno}: {first.value}")
        assert not literals, "run_gate 里出现规则 ID 字面量：" + "; ".join(literals)

    def test_gate_only_decides_on_has_error(self) -> None:
        """放行判定只看 has_error（NFR-10.5），不给调用方别的门槛。"""
        result = run_gate(_snap(), GateContext())
        assert result.has_error is False
        assert set(vars(result)) == {"findings", "has_error"}
