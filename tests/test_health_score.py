"""E-01 健康度合成口径（FR-1.0.24 / NFR-10.3 / T11）。

对应 `docs/11-功能演进需求-V2.md` E-01.1~E-01.5 的 GWT 与静态约束。
"""

from __future__ import annotations

import ast
from pathlib import Path

from skillos.core.algorithm.checks import CheckLevel, RuleFinding, summarize_health
from skillos.core.constants import HEALTH_SCORE_MAX, HEALTH_WEIGHTS

CONSTANTS_PY = Path(__file__).resolve().parents[1] / "skillos" / "core" / "constants.py"


def _errors(n: int) -> list[RuleFinding]:
    return [RuleFinding(f"r{i}", CheckLevel.ERROR, "m") for i in range(n)]


class TestE01Score:
    def test_gwt_2error_1warn_1degraded_equals_69(self) -> None:
        """E-01.1 GWT：100 − 10×2 − 3×1 − 8×1 = 69。"""
        findings = _errors(2) + [RuleFinding("w", CheckLevel.WARN, "m")]
        decay = {
            "staleness": "degraded",
            "dependency_broken": "ok",
            "upstream_divergence": "unknown",
        }
        h = summarize_health(findings, decay)
        assert h["score"] == 69
        assert h["degraded_decay_dimensions"] == 1
        assert h["deductions"] == [
            {"kind": "error", "count": 2, "points": 20},
            {"kind": "warn", "count": 1, "points": 3},
            {"kind": "decay", "dimension": "staleness", "count": 1, "points": 8},
        ]

    def test_gwt_floor_is_zero(self) -> None:
        """E-01.1 GWT：error 多到 15 个时 score 取 0，不下溢为负。"""
        assert summarize_health(_errors(15))["score"] == 0

    def test_unknown_dimension_costs_nothing_but_is_reported(self) -> None:
        """E-01.3 GWT：上游跟踪未启用 ⇒ unknown 不扣分，但「未知 N 项」要报出来。"""
        decay = {
            "staleness": "ok",
            "dependency_broken": "ok",
            "upstream_divergence": "unknown",
        }
        h = summarize_health([], decay)
        assert h["score"] == HEALTH_SCORE_MAX
        assert h["unknown_dimensions"] == 1
        assert h["unknown_dimension_names"] == ["upstream_divergence"]

    def test_recompute_is_idempotent(self) -> None:
        """E-01.4：同一输入两次计算同分。"""
        findings = _errors(1) + [RuleFinding("w", CheckLevel.WARN, "m")]
        decay = {"staleness": "degraded", "dependency_broken": "ok"}
        assert summarize_health(findings, decay) == summarize_health(findings, decay)

    def test_legacy_flat_decay_shape_same_conclusion(self) -> None:
        """P3 扁平形态（staleness_days / dependency_broken 列表）与结构化形态同结论。"""
        legacy = summarize_health([], {"staleness_days": 200.0, "dependency_broken": []})
        structured = summarize_health(
            [], {"staleness": "degraded", "dependency_broken": "ok"}
        )
        assert legacy["score"] == structured["score"] == 100 - HEALTH_WEIGHTS["degraded_decay"]
        assert legacy["degraded_decay_dimensions"] == 1


class TestE01WeightContract:
    def test_weights_are_the_documented_values(self) -> None:
        """E-01.2：权重就是这三个数，改权重 = 改代码（提交说明里能看见）。"""
        assert HEALTH_WEIGHTS == {"error": 10, "warn": 3, "degraded_decay": 8}

    def test_weights_not_configurable_at_runtime(self) -> None:
        """E-01.2：`HEALTH_WEIGHTS` 定义处不得读环境变量 / 配置。

        运行时可配会让历史分数不可比（分不清是资产变了还是权重变了），
        所以这里用 AST 静态断言，而不是靠人眼 review。
        """
        tree = ast.parse(CONSTANTS_PY.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign):
                target = node.target
                name = target.id if isinstance(target, ast.Name) else None
                value_node = node.value
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                name = names[0] if "HEALTH_WEIGHTS" in names else None
                value_node = node.value
            else:
                continue
            if name != "HEALTH_WEIGHTS" or value_node is None:
                continue
            dumped = ast.dump(value_node)
            assert "environ" not in dumped, "HEALTH_WEIGHTS 不得读环境变量（E-01.2）"
            assert "getenv" not in dumped, "HEALTH_WEIGHTS 不得读环境变量（E-01.2）"
            assert isinstance(value_node, ast.Dict), "HEALTH_WEIGHTS 必须是字面量 dict"
            return
        raise AssertionError("core/constants.py 里找不到 HEALTH_WEIGHTS 定义")
