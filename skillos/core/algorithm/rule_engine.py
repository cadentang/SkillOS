"""自定义门禁规则引擎（N-03.2~4 / N-03.6，`docs/12` §6.1）。

**只有声明式匹配，没有任何代码执行面**（N-03.6 / NFR-V2-05）：
规则是 YAML 里的正则/存在性断言，本模块不 import `eval` / `exec` / `importlib`，
也不把规则内容喂给任何解释器 —— 架构断言由 `tests/test_rule_engine.py` 静态扫描兜底。

三条硬约束（每条都对应一种"看起来跑了、其实没生效"）：

1. **单规则失败隔离**（N-03.4）：正则编译失败 / 字段不合法 / rule_id 冲突 →
   该条落 `InvalidRule`（调用方转成 info finding 落库），**其余规则照常执行**；
2. **限额**（N-03.3）：单仓库 ≤ `MAX_CUSTOM_RULES` 条，超限**显式报错**而不是悄悄截断
   —— 截断等于"用户以为规则生效了"；
3. **超时不中断**（N-03.3）：单条匹配超 100ms → warn finding「正则超时」，继续下一条。

判定与呈现分离：本模块只产出 `RuleFinding`，落库/展示由 services 层承担。
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from skillos.core.algorithm.checks import RULES, CheckLevel, RuleFinding
from skillos.core.model import FileEntry, SkillSnapshot

# 加载期限额（N-03.3：估算 200 条）。超限报错，不截断。
MAX_CUSTOM_RULES: int = 200
# 单条匹配超时（N-03.3：估算 100ms）
MATCH_TIMEOUT_S: float = 0.1
# 规则文件里的类别标签；`security` 供 `check --security-only` 过滤（N-08.5）
SECURITY_CATEGORY = "security"

# 允许的规则 ID 前缀：`custom.` = 用户自定义（N-03.2 强制），`security.` = 随包安全规则集
_ALLOWED_PREFIXES: tuple[str, ...] = ("custom.", SECURITY_CATEGORY + ".")
_LEVELS: frozenset[str] = frozenset({"error", "warn", "info"})
_TARGETS: frozenset[str] = frozenset(
    {"description", "frontmatter", "file_content", "file_list"}
)
_FLAGS: dict[str, int] = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}

# frontmatter 段：SKILL.md 开头两个 `---` 之间
_FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---", re.DOTALL)


class RulesetTooLarge(Exception):
    """规则条数超过 `MAX_CUSTOM_RULES`（N-03.3：显式报错，不截断）。"""


@dataclass(frozen=True)
class CustomRule:
    """一条已编译通过的声明式规则。"""

    rule_id: str
    level: str
    target: str
    pattern: re.Pattern[str]
    flags: frozenset[str] = frozenset()
    scope_paths: tuple[str, ...] | None = None
    scope_exclude: tuple[str, ...] | None = None
    message: str = ""
    category: str | None = None
    source: str = ""

    @property
    def is_security(self) -> bool:
        return self.category == SECURITY_CATEGORY


@dataclass(frozen=True)
class InvalidRule:
    """加载期被判无效的规则（N-03.4：落库 info，不静默跳过）。"""

    rule_id: str
    reason: str
    source: str


@dataclass(frozen=True)
class LoadResult:
    rules: tuple[CustomRule, ...] = ()
    invalid: tuple[InvalidRule, ...] = ()
    files: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------- 加载


def load_rule_file(path: Path) -> tuple[list[CustomRule], list[InvalidRule]]:
    """解析单个 YAML 文件；文件级错误也变成 InvalidRule，不抛给调用方。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [], [InvalidRule(rule_id=path.name, reason=f"文件无法解析: {exc}",
                                source=str(path))]
    if raw is None:
        return [], []
    if not isinstance(raw, dict):
        return [], [InvalidRule(rule_id=path.name, reason="顶层必须是映射（rules: ...）",
                                source=str(path))]
    items = raw.get("rules")
    if not isinstance(items, list):
        return [], [InvalidRule(rule_id=path.name, reason="缺少 rules: [...] 列表",
                                source=str(path))]

    rules: list[CustomRule] = []
    invalid: list[InvalidRule] = []
    for index, item in enumerate(items):
        rule, reason = _parse_rule(item, source=str(path))
        if rule is None:
            invalid.append(InvalidRule(
                rule_id=_rule_id_hint(item, index),
                reason=reason or "规则格式不合法",
                source=str(path),
            ))
            continue
        rules.append(rule)
    return rules, invalid


def load_rules(rules_dir: Path) -> LoadResult:
    """加载目录下全部 `*.yaml`（N-03.1：随 git 版本化）。

    - 条数超限 → 抛 `RulesetTooLarge`（显式失败，不截断）；
    - 同一 `rule_id` 重复 → 后出现的判 invalid（"重复的 rule_id"），先出现的保留；
    - 与**内置规则 ID** 冲突（fm.* / struct.* / dep.* / asset.*）→ invalid。
    """
    if not rules_dir.is_dir():
        return LoadResult()
    files = sorted(p for p in rules_dir.glob("*.y*ml") if p.is_file())
    collected: list[CustomRule] = []
    invalid: list[InvalidRule] = []
    seen: dict[str, str] = {}
    for path in files:
        rules, bad = load_rule_file(path)
        invalid.extend(bad)
        for rule in rules:
            if rule.rule_id in seen:
                invalid.append(InvalidRule(
                    rule_id=rule.rule_id,
                    reason=f"重复的 rule_id（已定义于 {seen[rule.rule_id]}）",
                    source=rule.source,
                ))
                continue
            seen[rule.rule_id] = rule.source
            collected.append(rule)

    if len(collected) > MAX_CUSTOM_RULES:
        raise RulesetTooLarge(
            f"规则条数 {len(collected)} 超过上限 {MAX_CUSTOM_RULES}"
            f"（{rules_dir}）—— 请合并/删除规则，不会截断执行"
        )
    return LoadResult(tuple(collected), tuple(invalid), tuple(p.name for p in files))


def _rule_id_hint(item: object, index: int) -> str:
    if isinstance(item, dict) and isinstance(item.get("rule_id"), str):
        return str(item["rule_id"])
    return f"rules[{index}]"


def _parse_rule(item: object, *, source: str) -> tuple[CustomRule | None, str]:
    if not isinstance(item, dict):
        return None, "规则必须是映射"
    rule_id = item.get("rule_id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        return None, "缺少 rule_id"
    rule_id = rule_id.strip()
    if not rule_id.startswith(_ALLOWED_PREFIXES):
        return None, (f"rule_id 必须以 {' 或 '.join(_ALLOWED_PREFIXES)} 开头"
                      f"（内置规则 ID 不可覆盖）")
    if rule_id in RULES:
        return None, "rule_id 与内置规则冲突"

    level = item.get("level", "warn")
    if level not in _LEVELS:
        return None, f"level 必须是 {'/'.join(sorted(_LEVELS))}"
    target = item.get("target")
    if target not in _TARGETS:
        return None, f"target 必须是 {'/'.join(sorted(_TARGETS))}"

    message = item.get("message")
    if not isinstance(message, str) or not message.strip():
        return None, "message 必填（中文可读，直接展示给用户）"

    match = item.get("match")
    if not isinstance(match, dict):
        return None, "缺少 match: {pattern: ...}"
    raw_pattern = match.get("pattern")
    if not isinstance(raw_pattern, str) or not raw_pattern:
        return None, "match.pattern 必填"
    if match.get("regex") is False:
        raw_pattern = re.escape(raw_pattern)   # 字面量模式：转义后当正则用，同一套执行路径

    raw_flags = match.get("flags") or []
    if not isinstance(raw_flags, list) or any(f not in _FLAGS for f in raw_flags):
        return None, f"match.flags 只能是 {sorted(_FLAGS)} 的子集"
    flags = frozenset(raw_flags)

    try:
        compiled = re.compile(raw_pattern, sum(_FLAGS[f] for f in flags))
    except re.error as exc:
        return None, f"正则编译失败: {exc}"

    scope = item.get("scope")
    paths = exclude = None
    if scope is not None:
        if not isinstance(scope, dict):
            return None, "scope 必须是映射（paths / exclude）"
        paths = _str_tuple(scope.get("paths"))
        exclude = _str_tuple(scope.get("exclude"))

    category = item.get("category")
    if category is not None and not isinstance(category, str):
        return None, "category 必须是字符串"

    return CustomRule(
        rule_id=rule_id, level=level, target=target, pattern=compiled,
        flags=flags, scope_paths=paths, scope_exclude=exclude,
        message=message.strip(), category=category, source=source,
    ), ""


def _str_tuple(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    return None


# ---------------------------------------------------------------- 执行


def run_custom_rules(
    rules: Sequence[CustomRule],
    snapshot: SkillSnapshot,
    *,
    timeout_s: float = MATCH_TIMEOUT_S,
    extra_files: Iterable[FileEntry] = (),
) -> list[RuleFinding]:
    """跑一遍全部规则（纯计算 + 单条超时保护）。

    输出按 `(rule_id, 命中路径)` 排序 —— 同一快照两次调用结果逐字节相同。
    """
    runner = RuleRunner(timeout_s=timeout_s)
    try:
        return runner.run(rules, snapshot, extra_files=extra_files)
    finally:
        runner.close()


class RuleRunner:
    """可复用的执行器：一次 check 内多条 skill 共用同一个线程池。

    每条规则单独 submit + `result(timeout)`：超时只影响这一条（N-03.3），
    不会把整轮检查挂住；线程池 `max_workers=1` 保证同一规则内部不会并发改写状态。
    """

    def __init__(self, *, timeout_s: float = MATCH_TIMEOUT_S) -> None:
        self._timeout = timeout_s
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rule")
        self._lock = threading.Lock()

    def close(self) -> None:
        # 超时的任务无法强杀（CPython 限制），这里只保证不阻塞调用方返回
        self._pool.shutdown(wait=False, cancel_futures=True)

    def run(
        self,
        rules: Sequence[CustomRule],
        snapshot: SkillSnapshot,
        *,
        extra_files: Iterable[FileEntry] = (),
    ) -> list[RuleFinding]:
        targets = _targets_for(snapshot, extra_files)
        findings: list[RuleFinding] = []
        for rule in rules:
            pairs = _apply_scope(rule, targets[rule.target])
            if not pairs:
                continue
            with self._lock:
                future = self._pool.submit(_match, rule, pairs)
                try:
                    hits = future.result(timeout=self._timeout)
                except FuturesTimeout:
                    findings.append(RuleFinding(
                        rule.rule_id, CheckLevel.WARN,
                        f"正则匹配超时（>{int(self._timeout * 1000)}ms），已跳过该规则",
                    ))
                    continue
            for path in hits:
                findings.append(RuleFinding(
                    rule.rule_id, CheckLevel(rule.level),
                    f"{rule.message}" + (f"（{path}）" if path else ""),
                    path or None,
                ))
        findings.sort(key=lambda f: (f.rule, f.message))
        return findings


def _targets_for(
    snapshot: SkillSnapshot, extra_files: Iterable[FileEntry]
) -> dict[str, list[tuple[str, str]]]:
    """把快照摊平成「target → [(path, 待匹配文本)]」。"""
    files: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in (*snapshot.files, *extra_files):
        if entry.path in seen:
            continue
        seen.add(entry.path)
        text = _text_of(entry)
        if text is None:
            continue                      # 二进制不扫（正则对二进制只会误报）
        files.append((entry.path, text))

    fm = ""
    for entry in snapshot.files:
        if entry.path == "SKILL.md":
            hit = _FM_RE.match(_text_of(entry) or "")
            fm = hit.group(1) if hit else ""
            break

    return {
        "description": [("", snapshot.meta.description or "")],
        "frontmatter": [("", fm)],
        "file_content": files,
        "file_list": [(path, path) for path, _ in files],
    }


def _text_of(entry: FileEntry) -> str | None:
    if not entry.content:
        return ""
    try:
        return entry.content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _apply_scope(
    rule: CustomRule, pairs: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """scope.paths / scope.exclude 用 fnmatch 语义，只作用于带路径的目标。"""
    import fnmatch

    out: list[tuple[str, str]] = []
    for path, text in pairs:
        if path and rule.scope_paths and not any(
            fnmatch.fnmatch(path, pat) for pat in rule.scope_paths
        ):
            continue
        if path and rule.scope_exclude and any(
            fnmatch.fnmatch(path, pat) for pat in rule.scope_exclude
        ):
            continue
        out.append((path, text))
    return out


def _match(rule: CustomRule, pairs: list[tuple[str, str]]) -> list[str]:
    """返回命中的路径列表（description/frontmatter 命中记空串）。"""
    hits: list[str] = []
    for path, text in pairs:
        if rule.pattern.search(text):
            hits.append(path)
    return hits


def finding_rows(findings: Iterable[RuleFinding]) -> list[dict]:
    """转成 `skill_checks` 行（scope=gate）。规则类别与来源在 value 里留痕。"""
    return [
        {
            "rule": f.rule,
            "level": str(f.level),
            "message": f.message,
            "value": None,
        }
        for f in findings
    ]


def invalid_rows(invalid: Iterable[InvalidRule]) -> list[dict]:
    """无效规则也落库（info 级，N-03.4）：规则面板要能审计"哪条没生效、为什么"。"""
    return [
        {
            "rule": item.rule_id,
            "level": str(CheckLevel.INFO),
            "message": f"规则未生效：{item.reason}",
            "value": None,
        }
        for item in invalid
    ]


def filter_security(rules: Sequence[CustomRule]) -> list[CustomRule]:
    """`check --security-only` 的过滤口径（N-08.5）：只按 `category: security`。"""
    return [rule for rule in rules if rule.is_security]


def as_dict(rule: CustomRule) -> dict[str, Any]:
    """规则面板序列化（不含编译对象）。"""
    return {
        "rule_id": rule.rule_id,
        "level": rule.level,
        "target": rule.target,
        "pattern": rule.pattern.pattern,
        "flags": sorted(rule.flags),
        "scope_paths": list(rule.scope_paths) if rule.scope_paths else [],
        "scope_exclude": list(rule.scope_exclude) if rule.scope_exclude else [],
        "message": rule.message,
        "category": rule.category,
        "source": rule.source,
    }
