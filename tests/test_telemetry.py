"""V2-M2-05：token 归属纯函数 + Claude Code 适配器（N-11.3 / N-11.4 / N-11.8）。

- 归属：时间片口径，唯一事实源是 `docs/11` N-11.3 那一句话；
- 解析：坏行跳过并计数、只有 assistant 行算 usage、路径命中才归属；
- 配置：适配器是数据不是代码（V2-T7），YAML 缺失要报错而不是返回空配置。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from skillos.core.algorithm.telemetry import (
    UNATTRIBUTED,
    CallEvent,
    UsageSlice,
    attribute_tokens,
)
from skillos.core.algorithm.telemetry_parser import (
    ADAPTER_DIR,
    AdapterSpec,
    TelemetryAdapterError,
    available_adapters,
    compile_skill_patterns,
    dotted_get,
    expand_autodetect,
    load_spec,
    parse_lines,
)

T0 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
DEPLOY_ROOT = Path("D:/work/.claude/skills")


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


class TestAttribution:
    def test_gwt_slices_before_first_call_are_unattributed(self) -> None:
        result = attribute_tokens(
            events=[CallEvent(at=_at(10), skill_id="alpha")],
            slices=[
                UsageSlice(at=_at(0), input_tokens=5, output_tokens=1),
                UsageSlice(at=_at(20), input_tokens=7, output_tokens=3),
            ],
        )
        assert result.by_skill["alpha"].input_tokens == 7
        assert result.by_skill["alpha"].output_tokens == 3
        assert result.unattributed.input_tokens == 5

    def test_switch_between_skills(self) -> None:
        """N-11.3：之后的 usage 归该 skill，直到**下一个不同 skill**的调用事件。"""
        result = attribute_tokens(
            events=[CallEvent(at=_at(0), skill_id="alpha"), CallEvent(at=_at(30), skill_id="beta")],
            slices=[
                UsageSlice(at=_at(5), input_tokens=10),
                UsageSlice(at=_at(25), input_tokens=20),
                UsageSlice(at=_at(40), input_tokens=30),
            ],
        )
        assert result.by_skill["alpha"].input_tokens == 30     # 10 + 20
        assert result.by_skill["beta"].input_tokens == 30

    def test_same_timestamp_call_wins_over_slice(self) -> None:
        """同一时间戳时调用事件先于 usage，否则这次调用的 token 会被判成未归属。"""
        result = attribute_tokens(
            events=[CallEvent(at=_at(10), skill_id="alpha")],
            slices=[UsageSlice(at=_at(10), input_tokens=4)],
        )
        assert result.by_skill["alpha"].input_tokens == 4
        assert result.unattributed.input_tokens == 0

    def test_no_calls_means_everything_unattributed(self) -> None:
        result = attribute_tokens(
            events=[],
            slices=[UsageSlice(at=_at(0), input_tokens=9), UsageSlice(at=_at(60), input_tokens=1)],
        )
        assert result.by_skill == {}
        assert result.unattributed.input_tokens == 10
        assert UNATTRIBUTED == "unattributed"

    def test_input_order_does_not_matter(self) -> None:
        events = [CallEvent(at=_at(30), skill_id="beta"), CallEvent(at=_at(0), skill_id="alpha")]
        slices = [UsageSlice(at=_at(40), input_tokens=2), UsageSlice(at=_at(5), input_tokens=3)]
        a = attribute_tokens(events, slices)
        b = attribute_tokens(list(reversed(events)), list(reversed(slices)))
        assert {k: v.as_dict() for k, v in a.by_skill.items()} == \
            {k: v.as_dict() for k, v in b.by_skill.items()}


def _line(**payload) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _assistant(ts: str, *, content: str = "", tokens: int = 10) -> str:
    return _line(
        type="assistant",
        timestamp=ts,
        message={
            "content": content,
            "usage": {"input_tokens": tokens, "output_tokens": tokens // 2,
                      "cache_read_input_tokens": 1},
        },
    )


class TestClaudeCodeAdapter:
    def test_spec_is_data_driven(self) -> None:
        spec = load_spec("claude-code")
        assert spec.runtime == "claude-code"
        assert spec.usage_event_types() == frozenset({"assistant"})
        assert spec.field("input_tokens") == "message.usage.input_tokens"
        assert "claude-code" in available_adapters()
        assert ADAPTER_DIR.is_dir()

    def test_missing_adapter_raises(self) -> None:
        with pytest.raises(TelemetryAdapterError):
            load_spec("no-such-runtime")

    def test_bad_usage_field_map_raises(self) -> None:
        spec = AdapterSpec(runtime="x", autodetect=(), session_glob="*.jsonl",
                           usage_field_map={}, skill_match=())
        with pytest.raises(TelemetryAdapterError):
            spec.field("input_tokens")

    def test_dotted_get_is_missing_safe(self) -> None:
        assert dotted_get({"a": {"b": 1}}, "a.b") == 1
        assert dotted_get({"a": {}}, "a.b.c") is None
        assert dotted_get({"a": [1, 2]}, "a.0") is None

    def test_expand_autodetect_substitutes_home(self) -> None:
        spec = load_spec("claude-code")
        paths = expand_autodetect(spec, Path("C:/Users/me"))
        normalized = str(paths[0]).replace("\\", "/")
        assert normalized.endswith(".claude/projects")
        assert "C:/Users/me" in normalized

    def test_gwt_parse_usage_and_skill_hit(self) -> None:
        """N-11.4：会话行命中部署目录 → 归属该 slug；usage 抽成切片。"""
        spec = load_spec("claude-code")
        lines = [
            _assistant("2026-10-24T09:00:10Z", content=f"edited {DEPLOY_ROOT}/alpha/SKILL.md"),
            _assistant("2026-10-24T09:00:20Z", tokens=4),
        ]
        result = parse_lines(lines, spec, deploy_roots=[DEPLOY_ROOT])
        assert [c.skill_id for c in result.calls] == ["alpha"]
        assert len(result.slices) == 2
        assert result.skipped_lines == 0
        assert result.usage_lines == 2

        attribution = attribute_tokens(result.calls, result.slices)
        assert set(attribution.by_skill) == {"alpha"}

    def test_user_lines_do_not_produce_usage(self) -> None:
        spec = load_spec("claude-code")
        lines = [
            _line(type="user", timestamp="2026-10-24T09:00:00Z",
                  message={"content": "hi", "usage": {"input_tokens": 999}}),
        ]
        result = parse_lines(lines, spec, deploy_roots=[DEPLOY_ROOT])
        assert result.slices == []
        assert result.usage_lines == 0

    def test_broken_lines_are_skipped_and_counted(self) -> None:
        """N-11.8：损坏行跳过并计数 —— 不许中断整次 import，也不许静默吞掉。"""
        spec = load_spec("claude-code")
        lines = [
            "{ not json",                                             # 坏 JSON
            _line(type="assistant", message={"usage": {"input_tokens": 3}}),  # 无时间戳
            "[1, 2, 3]\n",                                            # 非对象
            _assistant("2026-10-24T09:00:30Z"),
        ]
        result = parse_lines(lines, spec, deploy_roots=[DEPLOY_ROOT])
        assert result.total_lines == 4
        assert result.skipped_lines == 3
        assert len(result.slices) == 1

    def test_no_skill_hit_does_not_force_attribution(self) -> None:
        """N-11.4：未命中不强行归属 —— token 进 unattributed，而不是挂到随便某个 skill。"""
        spec = load_spec("claude-code")
        result = parse_lines(
            [_assistant("2026-10-24T09:00:10Z", content="nothing to see")],
            spec, deploy_roots=[DEPLOY_ROOT],
        )
        attribution = attribute_tokens(result.calls, result.slices)
        assert attribution.by_skill == {}
        assert attribution.unattributed.total > 0

    def test_without_deploy_roots_falls_back_to_repo_skills(self) -> None:
        spec = load_spec("claude-code")
        patterns = compile_skill_patterns(spec, [])
        hit = patterns[0].search("see skills/alpha/SKILL.md")
        assert hit and hit.group("slug") == "alpha"

    def test_tool_use_fields_are_scanned(self) -> None:
        spec = load_spec("claude-code")
        line = _line(
            type="assistant",
            timestamp="2026-10-24T09:00:10Z",
            toolUse={"parameters": {"path": f"{DEPLOY_ROOT}/beta/SKILL.md"}},
            message={"usage": {"input_tokens": 2}},
        )
        result = parse_lines([line], spec, deploy_roots=[DEPLOY_ROOT])
        assert [c.skill_id for c in result.calls] == ["beta"]
