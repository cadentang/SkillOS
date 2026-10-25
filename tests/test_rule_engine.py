"""N-03 规则引擎（`docs/12` §6.1）：加载隔离 / 限额 / 超时 / 无代码执行。

三条最容易被实现成"看起来跑了"的地方：
1. 正则编译失败被静默跳过 → 用户以为规则生效（必须落 invalid）；
2. 条数超限被截断 → 尾部规则永远不跑（必须报错）；
3. 规则内容被当成代码执行 → 安全叙事当场失效（必须静态可证）。
"""

from __future__ import annotations

import pytest

from skillos.core.algorithm.rule_engine import (
    MAX_CUSTOM_RULES,
    CustomRule,
    RulesetTooLarge,
    load_rules,
    run_custom_rules,
)
from skillos.core.model import FileEntry, SkillMeta, SkillSnapshot


def _snapshot(files: dict[str, str] | None = None, *, description: str = "demo") -> SkillSnapshot:
    entries = [
        FileEntry(path=path, content=text.encode("utf-8"))
        for path, text in (
            files or {"SKILL.md": "---\nname: a\ndescription: demo\n---\nbody"}
        ).items()
    ]
    return SkillSnapshot(
        skill_id="s1", slug="a",
        meta=SkillMeta(name="a", description=description),
        files=tuple(entries),
    )


def _write(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


class TestLoading:
    def test_valid_rule_loads(self, tmp_path) -> None:
        _write(tmp_path, "a.yaml", """
rules:
  - rule_id: custom.no-secrets
    level: error
    target: file_content
    match: {pattern: "AKIA[0-9A-Z]{16}"}
    message: 不要写密钥
""")
        loaded = load_rules(tmp_path)
        assert [r.rule_id for r in loaded.rules] == ["custom.no-secrets"]
        assert not loaded.invalid
        assert loaded.rules[0].level == "error"

    def test_prefix_is_enforced(self, tmp_path) -> None:
        """N-03.2：必须 custom./security. 前缀，内置 ID 不可覆盖。"""
        _write(tmp_path, "bad.yaml", """
rules:
  - rule_id: fm.description.length
    level: error
    target: description
    match: {pattern: "." }
    message: 冒名顶替
  - rule_id: my-rule
    level: warn
    target: description
    match: {pattern: "." }
    message: 缺前缀
""")
        loaded = load_rules(tmp_path)
        assert loaded.rules == ()
        assert len(loaded.invalid) == 2
        assert all(i.reason and "rule_id" in i.reason for i in loaded.invalid)

    def test_regex_error_is_isolated(self, tmp_path) -> None:
        """N-03.4：单条编译失败 → 该条 invalid，其余规则照常。"""
        _write(tmp_path, "mix.yaml", """
rules:
  - rule_id: custom.broken
    level: error
    target: file_content
    match: {pattern: "([unclosed"}
    message: 坏正则
  - rule_id: custom.ok
    level: warn
    target: description
    match: {pattern: "demo"}
    message: 正常规则
""")
        loaded = load_rules(tmp_path)
        assert [r.rule_id for r in loaded.rules] == ["custom.ok"]
        assert [i.rule_id for i in loaded.invalid] == ["custom.broken"]
        assert "正则编译失败" in loaded.invalid[0].reason

    def test_missing_message_is_invalid(self, tmp_path) -> None:
        _write(tmp_path, "m.yaml", """
rules:
  - rule_id: custom.no-message
    level: warn
    target: description
    match: {pattern: "x"}
""")
        loaded = load_rules(tmp_path)
        assert loaded.rules == ()
        assert "message" in loaded.invalid[0].reason

    def test_duplicate_rule_id_keeps_first(self, tmp_path) -> None:
        _write(tmp_path, "d.yaml", """
rules:
  - rule_id: custom.dup
    level: warn
    target: description
    match: {pattern: "a"}
    message: 第一条
  - rule_id: custom.dup
    level: error
    target: description
    match: {pattern: "b"}
    message: 第二条
""")
        loaded = load_rules(tmp_path)
        assert len(loaded.rules) == 1 and loaded.rules[0].message == "第一条"
        assert "重复的 rule_id" in loaded.invalid[0].reason

    def test_limit_raises_instead_of_truncating(self, tmp_path) -> None:
        """N-03.3：超限显式报错，绝不悄悄截断。"""
        body = "rules:\n" + "".join(
            f"  - rule_id: custom.r{i}\n    level: warn\n    target: description\n"
            f"    match: {{pattern: \"x{i}\"}}\n    message: m\n"
            for i in range(MAX_CUSTOM_RULES + 1)
        )
        _write(tmp_path, "big.yaml", body)
        with pytest.raises(RulesetTooLarge):
            load_rules(tmp_path)

    def test_broken_yaml_is_invalid_not_crash(self, tmp_path) -> None:
        _write(tmp_path, "broken.yaml", "rules: [ {")
        loaded = load_rules(tmp_path)
        assert loaded.rules == ()
        assert "无法解析" in loaded.invalid[0].reason


class TestRunning:
    def _rule(self, **over) -> CustomRule:
        import re
        base = dict(
            rule_id="custom.t", level="error", target="file_content",
            pattern=re.compile("SECRET"), message="命中",
        )
        base.update(over)
        return CustomRule(**base)

    def test_file_content_hit_carries_path(self) -> None:
        findings = run_custom_rules(
            [self._rule()], _snapshot({"SKILL.md": "SECRET=1", "refs/a.md": "ok"}),
        )
        assert len(findings) == 1
        assert findings[0].rule == "custom.t" and findings[0].path == "SKILL.md"
        assert str(findings[0].level) == "error"

    def test_description_target(self) -> None:
        findings = run_custom_rules(
            [self._rule(target="description")], _snapshot(description="SECRET here"),
        )
        assert len(findings) == 1 and findings[0].path is None

    def test_scope_paths_and_exclude(self) -> None:
        snap = _snapshot({"SKILL.md": "SECRET", "docs/a.md": "SECRET", "bin/x.py": "SECRET"})
        scoped = self._rule(scope_paths=["docs/**"])
        assert [f.path for f in run_custom_rules([scoped], snap)] == ["docs/a.md"]

        excluded = self._rule(scope_exclude=["docs/**"])
        touched = sorted(f.path for f in run_custom_rules([excluded], snap))
        assert touched == ["SKILL.md", "bin/x.py"]

    def test_binary_files_are_skipped(self) -> None:
        snap = SkillSnapshot(
            skill_id="s1", slug="a",
            meta=SkillMeta(name="a", description="demo"),
            files=(FileEntry(path="SKILL.md", content=b"\xff\xfe\x00SECRET"),),
        )
        assert run_custom_rules([self._rule()], snap) == []

    def test_timeout_reports_warn_without_breaking(self, monkeypatch) -> None:
        """N-03.3：单条超时 → warn finding，其他规则照常出结果。

        用「睡一会儿的假匹配器」而不是灾难性回溯正则：回溯线程杀不掉，
        会把测试进程一起拖住 —— 那验证的是 CPython 限制，不是我们的行为。
        """
        import time

        from skillos.core.algorithm import rule_engine as engine_module

        real_match = engine_module._match

        def fake_match(rule, pairs):
            if rule.rule_id == "custom.slow":
                time.sleep(0.3)
                return []
            return real_match(rule, pairs)

        monkeypatch.setattr(engine_module, "_match", fake_match)

        import re

        slow = self._rule(rule_id="custom.slow", pattern=re.compile("x"), message="慢")
        fast = self._rule(rule_id="custom.fast", pattern=re.compile("needle"), message="快")
        findings = run_custom_rules([slow, fast], _snapshot({"SKILL.md": "needle"}),
                                    timeout_s=0.05)

        rules = [f.rule for f in findings]
        assert "custom.fast" in rules
        assert any(f.rule == "custom.slow" and "超时" in f.message for f in findings)

    def test_results_are_deterministic(self) -> None:
        rules = [self._rule(rule_id="custom.b"), self._rule(rule_id="custom.a")]
        snap = _snapshot({"SKILL.md": "SECRET", "b.md": "SECRET"})
        first = [(f.rule, f.path) for f in run_custom_rules(rules, snap)]
        second = [(f.rule, f.path) for f in run_custom_rules(rules, snap)]
        assert first == second


class TestNoCodeExecution:
    """N-03.6 / NFR-V2-05：引擎源码不得出现代码执行入口。"""

    def test_engine_source_has_no_exec_primitives(self) -> None:
        """按 AST 扫调用与 import 名（docstring 里提到这些词不算违规）。"""
        import ast
        import inspect

        from skillos.core.algorithm import rule_engine

        tree = ast.parse(inspect.getsource(rule_engine))
        forbidden_calls = {"eval", "exec", "compile", "__import__", "import_module"}
        forbidden_modules = {"importlib", "subprocess", "os.system", "pickle"}

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls, f"禁止调用 {node.func.id}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_modules, alias.name
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_modules, node.module

    def test_python_in_rule_is_matched_as_text(self, tmp_path) -> None:
        """规则文件里写 Python 片段 → 原样当文本匹配，不执行。"""
        marker = tmp_path / "pwned.txt"
        _write(tmp_path, "evil.yaml", """
rules:
  - rule_id: custom.evil
    level: warn
    target: file_content
    match:
      pattern: "__import__"
    message: 文本匹配
""")
        loaded = load_rules(tmp_path)
        evil = f"__import__('pathlib').Path({str(marker)!r}).write_text('x')"
        snap = _snapshot({"SKILL.md": evil})
        findings = run_custom_rules(loaded.rules, snap)
        assert findings and "文本匹配" in findings[0].message
        assert not marker.exists(), "规则内容不得被执行"
