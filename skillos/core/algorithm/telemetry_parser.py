"""Runtime 会话日志适配器加载与解析（N-11.4/5/9，V2-T7「路径即数据」）。

分工：
- **本模块**：把 YAML 配置 + 若干行 JSON 变成 `CallEvent` / `UsageSlice`（纯函数，喂什么解析什么）；
- `services/telemetry_service.py`（M2-06）：找文件、逐行流式读取、去重、落库。

三条硬约束：
1. **路径即数据**（T12 同源纪律）：解析规则全在 `adapters/telemetry/*.yaml`，新增 Runtime 不加代码；
2. **不落盘正文**（N-11.8）：扫描内容只在内存里做正则匹配，函数返回的只有数字与 slug；
3. **坏行跳过并计数**（N-11.8）：解析不了的行不许中断整次 import，也不许静默吞掉（要能报数）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from skillos.core.algorithm.telemetry import CallEvent, UsageSlice

ADAPTER_DIR = Path(__file__).with_name("adapters") / "telemetry"
DEFAULT_RUNTIME = "claude-code"


class TelemetryAdapterError(Exception):
    """适配器配置缺失 / 结构非法（不静默降级成「解析不到东西」）。"""


@dataclass(frozen=True)
class SkillMatchRule:
    scan_fields: tuple[str, ...]
    pattern: str
    pattern_without_roots: str


@dataclass(frozen=True)
class AdapterSpec:
    runtime: str
    autodetect: tuple[str, ...]
    session_glob: str
    usage_field_map: Mapping[str, Any]
    skill_match: tuple[SkillMatchRule, ...] = field(default_factory=tuple)

    # ---- 字段映射（缺失即视为配置错误，不猜默认路径）----
    def field(self, name: str) -> str:
        raw = self.usage_field_map.get(name)
        if not isinstance(raw, str) or not raw:
            raise TelemetryAdapterError(f"{self.runtime}: usage_field_map 缺少 {name}")
        return raw

    def usage_event_types(self) -> frozenset[str]:
        raw = self.usage_field_map.get("usage_event_types")
        if not isinstance(raw, list) or not raw:
            raise TelemetryAdapterError(f"{self.runtime}: 缺少 usage_event_types")
        return frozenset(str(x) for x in raw)


def load_spec(runtime: str = DEFAULT_RUNTIME, *, directory: Path | None = None) -> AdapterSpec:
    """按 runtime 名加载 YAML 配置。找不到就报错（不返回空配置冒充可用）。"""
    base = directory or ADAPTER_DIR
    path = base / f"{runtime}.yaml"
    if not path.exists():
        raise TelemetryAdapterError(f"没有 {runtime} 的遥测适配器配置: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TelemetryAdapterError(f"{path}: 顶层必须是 map")
    rules = tuple(
        SkillMatchRule(
            scan_fields=tuple(str(f) for f in item.get("scan_fields") or ()),
            pattern=str(item["pattern"]),
            pattern_without_roots=str(item.get("pattern_without_roots") or item["pattern"]),
        )
        for item in raw.get("skill_match") or ()
    )
    return AdapterSpec(
        runtime=str(raw.get("runtime") or runtime),
        autodetect=tuple(str(p) for p in raw.get("autodetect") or ()),
        session_glob=str(raw.get("session_glob") or "**/*.jsonl"),
        usage_field_map=dict(raw.get("usage_field_map") or {}),
        skill_match=rules,
    )


def available_adapters(directory: Path | None = None) -> list[str]:
    base = directory or ADAPTER_DIR
    return sorted(p.stem for p in base.glob("*.yaml"))


def dotted_get(obj: Any, path: str) -> Any:
    """`a.b.c` 取值；任何一段缺失返回 None（不抛 —— 会话日志字段随版本变）。"""
    cursor: Any = obj
    for part in path.split("."):
        if isinstance(cursor, Mapping):
            if part not in cursor:
                return None
            cursor = cursor[part]
        elif isinstance(cursor, Sequence) and not isinstance(cursor, (str, bytes)):
            return None     # 列表不做下标猜测
        else:
            return None
    return cursor


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _scan_text(value: Any) -> str:
    """把任意嵌套结构压成一段可搜索文本（只用于正则命中，不落盘）。"""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return " ".join(_scan_text(v) for v in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return " ".join(_scan_text(v) for v in value)
    return ""


def expand_autodetect(spec: AdapterSpec, home: Path) -> list[Path]:
    """展开 `{home}` 占位（N-11.9①：支持多路径）。"""
    return [Path(str(p).replace("{home}", str(home))) for p in spec.autodetect]


def compile_skill_patterns(
    spec: AdapterSpec, deploy_roots: Iterable[Path]
) -> list[re.Pattern[str]]:
    """把 `{deploy_roots}` 展开成 alternation；没有部署根时退回 skills/ 形式。"""
    roots = [str(Path(r)).replace("\\", "/").rstrip("/") + "/" for r in deploy_roots]
    patterns: list[re.Pattern[str]] = []
    for rule in spec.skill_match:
        if roots:
            alternation = "|".join(re.escape(r) for r in roots)
            patterns.append(re.compile(rule.pattern.replace("{deploy_roots}", alternation)))
        else:
            patterns.append(re.compile(rule.pattern_without_roots))
    return patterns


def _parse_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class ParseResult:
    """一次会话文件的解析结果（不含任何正文，N-11.8）。"""

    calls: list[CallEvent] = field(default_factory=list)
    slices: list[UsageSlice] = field(default_factory=list)
    total_lines: int = 0
    skipped_lines: int = 0
    usage_lines: int = 0

    def extend(self, other: ParseResult) -> None:
        self.calls.extend(other.calls)
        self.slices.extend(other.slices)
        self.total_lines += other.total_lines
        self.skipped_lines += other.skipped_lines
        self.usage_lines += other.usage_lines


def _match_slug(
    obj: Mapping[str, Any],
    spec: AdapterSpec,
    patterns: Sequence[re.Pattern[str]],
) -> str | None:
    for rule, pattern in zip(spec.skill_match, patterns, strict=True):
        for field_path in rule.scan_fields:
            hit = pattern.search(_scan_text(dotted_get(obj, field_path)))
            if hit and hit.group("slug"):
                return hit.group("slug")
    return None


def parse_lines(
    lines: Iterable[str],
    spec: AdapterSpec,
    *,
    deploy_roots: Iterable[Path] = (),
) -> ParseResult:
    """逐行解析一个会话文件（流式：调用方传文件迭代器，不整体读入内存）。"""
    usage_types = spec.usage_event_types()
    type_field = spec.field("event_type")
    in_field = spec.field("input_tokens")
    out_field = spec.field("output_tokens")
    cache_field = spec.field("cache_read")
    ts_field = spec.field("timestamp")
    patterns = compile_skill_patterns(spec, deploy_roots)

    result = ParseResult()
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        result.total_lines += 1
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            result.skipped_lines += 1
            continue
        if not isinstance(obj, Mapping):
            result.skipped_lines += 1
            continue

        at = _parse_timestamp(dotted_get(obj, ts_field))
        if at is None:
            # 没有时间戳 → 无法进入时间片归属；跳过并计数（不猜时间）
            result.skipped_lines += 1
            continue

        if str(dotted_get(obj, type_field) or "") in usage_types:
            usage = UsageSlice(
                at=at,
                input_tokens=_as_int(dotted_get(obj, in_field)),
                output_tokens=_as_int(dotted_get(obj, out_field)),
                cache_read=_as_int(dotted_get(obj, cache_field)),
            )
            if usage.input_tokens or usage.output_tokens or usage.cache_read:
                result.slices.append(usage)
                result.usage_lines += 1

        slug = _match_slug(obj, spec, patterns)
        if slug:
            result.calls.append(CallEvent(at=at, skill_id=slug))
    return result
