"""E-02 折旧三维判定（FR-1.0.23 / NFR-10.4）。

对应 `docs/11` E-02.1~E-02.7 的 GWT。纯函数，不需要 git / db fixture。
"""

from __future__ import annotations

from skillos.core.algorithm.decay import (
    FORMAT_UNKNOWN,
    GATE_SIGNAL_DEPS_FORMAT,
    DecayResult,
    DecayStatus,
    evaluate_decay,
)
from skillos.core.constants import (
    STALENESS_DEGRADED_DAYS,
    STALENESS_DEGRADED_DAYS_RELAXED,
)


def _eval(**over) -> DecayResult:
    base = dict(
        days_since_last_commit=10,
        inbound_refs=0,
        declared_deps=[],
        resolved_slugs={"a"},
        dependency_bindings_ok={},
        upstream_status=None,
    )
    base.update(over)
    return evaluate_decay(**base)


class TestStaleness:
    def test_fresh_skill_is_ok(self) -> None:
        r = _eval(days_since_last_commit=10)
        assert r.staleness is DecayStatus.OK
        assert r.value["threshold_days"] == STALENESS_DEGRADED_DAYS

    def test_gwt_200_days_no_refs_is_degraded(self) -> None:
        """E-02.1 GWT：200 天零引用 ⇒ degraded，且 value.days_since = 200。"""
        r = _eval(days_since_last_commit=200)
        assert r.staleness is DecayStatus.DEGRADED
        assert r.value["days_since"] == 200
        assert r.value["relaxed_by_refs"] is False

    def test_gwt_200_days_with_refs_relaxes_to_180(self) -> None:
        """E-02.2 GWT：被引用 2 次 ⇒ 阈值放宽到 180；200 > 180 仍 degraded，并标记放宽。"""
        r = _eval(days_since_last_commit=200, inbound_refs=2)
        assert r.value["threshold_days"] == STALENESS_DEGRADED_DAYS_RELAXED
        assert r.value["relaxed_by_refs"] is True
        assert r.staleness is DecayStatus.DEGRADED

    def test_relaxed_threshold_keeps_100_days_ok(self) -> None:
        """被引用的资产 100 天不算陈旧（放宽后 100 < 180）——「被引用的不算僵尸」。"""
        assert _eval(days_since_last_commit=100, inbound_refs=1).staleness is DecayStatus.OK

    def test_unknown_when_no_commit_history(self) -> None:
        """没有 commit 历史 ⇒ unknown（不猜，NFR-10.4）。"""
        r = _eval(days_since_last_commit=None)
        assert r.staleness is DecayStatus.UNKNOWN
        assert r.value["days_since"] is None

    def test_clock_skew_clamped_to_zero(self) -> None:
        """时钟回拨（负天数）按 0 处理并留痕，供调用方打 warning。"""
        r = _eval(days_since_last_commit=-5)
        assert r.staleness is DecayStatus.OK
        assert r.value["days_since"] == 0
        assert r.value["clock_skew"] is True


class TestDependencyBroken:
    def test_no_deps_is_ok(self) -> None:
        assert _eval(declared_deps=[]).dependency_broken is DecayStatus.OK

    def test_gwt_missing_slug_is_degraded(self) -> None:
        """E-02.3 GWT：声明的 slug 不在库中 ⇒ degraded，value.missing_deps 列出它。"""
        r = _eval(declared_deps=["api-automation"], resolved_slugs=set())
        assert r.dependency_broken is DecayStatus.DEGRADED
        assert r.value["missing_deps"] == ["api-automation"]

    def test_present_but_unbound_is_degraded(self) -> None:
        """E-02.3 第 ② 层：库内有，但目标 Runtime 上没有有效 binding ⇒ degraded。"""
        r = _eval(
            declared_deps=["api-automation"],
            resolved_slugs={"api-automation"},
            dependency_bindings_ok={"api-automation": False},
        )
        assert r.dependency_broken is DecayStatus.DEGRADED
        assert r.value["unbound_deps"] == ["api-automation"]

    def test_present_and_bound_is_ok(self) -> None:
        r = _eval(
            declared_deps=["api-automation"],
            resolved_slugs={"api-automation"},
            dependency_bindings_ok={"api-automation": True},
        )
        assert r.dependency_broken is DecayStatus.OK

    def test_undetermined_binding_is_unknown(self) -> None:
        """调用方没给判据 ⇒ unknown，不猜成 ok。"""
        r = _eval(declared_deps=["api-automation"], resolved_slugs={"api-automation"})
        assert r.dependency_broken is DecayStatus.UNKNOWN
        assert r.value["undetermined_deps"] == ["api-automation"]

    def test_gwt_string_instead_of_list_is_unknown_with_gate_signal(self) -> None:
        """E-02.4 GWT：`dependencies: "api-automation"`（非 list）⇒ unknown + gate info 信号。"""
        r = _eval(declared_deps=FORMAT_UNKNOWN)
        assert r.dependency_broken is DecayStatus.UNKNOWN
        assert r.value["gate_signal"] == GATE_SIGNAL_DEPS_FORMAT
        assert r.value["missing_deps"] == []


class TestUpstreamDivergence:
    def test_unknown_before_e08(self) -> None:
        """E-02.5：E-08 未启用 ⇒ 恒 unknown，且**不扣分**（unknown 不加减）。"""
        r = _eval(upstream_status=None)
        assert r.upstream_divergence is DecayStatus.UNKNOWN

    def test_diverged_is_degraded(self) -> None:
        assert _eval(upstream_status="diverged").upstream_divergence is DecayStatus.DEGRADED

    def test_unrecognized_value_is_unknown(self) -> None:
        assert _eval(upstream_status="??? ").upstream_divergence is DecayStatus.UNKNOWN


class TestResultShape:
    def test_to_dict_matches_health_input_contract(self) -> None:
        """落库 / 传给 summarize_health 的形态：三维状态字符串 + value 原始值。"""
        d = _eval(days_since_last_commit=200).to_dict()
        assert d["staleness"] == "degraded"
        assert d["dependency_broken"] == "ok"
        assert d["upstream_divergence"] == "unknown"
        assert d["value"]["days_since"] == 200

    def test_helpers_list_degraded_and_unknown(self) -> None:
        r = _eval(days_since_last_commit=200)
        assert r.degraded_dimensions() == ["staleness"]
        assert r.unknown_dimensions() == ["upstream_divergence"]

    def test_pure_function_is_idempotent(self) -> None:
        a = _eval(days_since_last_commit=200, inbound_refs=3, declared_deps=["a"],
                  dependency_bindings_ok={"a": True})
        b = _eval(days_since_last_commit=200, inbound_refs=3, declared_deps=["a"],
                  dependency_bindings_ok={"a": True})
        assert a == b
