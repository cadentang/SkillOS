"""门禁单一实现验收（E-03.4 / NFR-10.5）：check 与 push 前置判定逐字段相等。

"CLI 通过而 push 拦截" 是治理类产品最容易出的信任事故 —— 两处判定只要各自演进一次
就会分叉。这里不看代码长相，而是**实测两条路径各自跑出来的 findings**（含顺序）
是否逐字段相等。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import skillos.services.check_service as check_service_module
from skillos.services.engine import EnvPaths, LocalEngine

# 故意缺 description：命中 fm.description.required（error → push 必被拦）
_BAD_SKILL_MD = b"---\nname: demo\nversion: 1.2.0\n---\nbody\n"

# 声明一个库里不存在的依赖：命中 dep.missing（验证 gate 上下文的依赖接线）
_MISSING_DEP_MD = (
    "---\nname: demo\nversion: 1.2.0\n"
    "description: 一个足够长的描述用于通过长度校验，说明这个 skill 的用途与边界。\n"
    "dependencies: [ghost-skill]\n---\nbody\n"
).encode()


def _env(tmp_path: Path, skill_md: bytes) -> LocalEngine:
    home = tmp_path / "home"
    for rt in ("claude", "agents"):
        d = home / f".{rt}" / "skills" / "demo"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_bytes(skill_md)
    paths = EnvPaths(repo_root=tmp_path / "repo", db_path=tmp_path / "state" / "skillos.db")
    engine = LocalEngine(paths, home=home)
    engine.init_env()
    engine.import_skills()
    return engine


def _serialize(gate) -> list[dict]:
    return [
        {"rule": str(f.rule), "level": str(f.level), "message": f.message}
        for f in gate.findings
    ]


@pytest.fixture()
def spy(monkeypatch):
    """记录每一次 gate 判定的实际结果，供两条路径比对。

    打的是 `check_service.run_gate` —— 那是**唯一实现点**：CLI check 经
    `CheckService.gate_of` 进来，push / 文件写入 / 新建经 `engine._gate_of_snapshot`
    进来，两条路径最终都落在这一个符号上。补丁打在 engine 上会看不到 check 路径
    （engine 已不再直接调 `run_gate`），测试会假绿。
    """
    calls: list[list[dict]] = []
    real = check_service_module.run_gate

    def wrapper(snapshot, ctx, **kwargs):
        result = real(snapshot, ctx, **kwargs)
        calls.append(_serialize(result))
        return result

    monkeypatch.setattr(check_service_module, "run_gate", wrapper)
    return calls


class TestCheckPushConsistency:
    def test_findings_identical_between_check_and_push(self, tmp_path, spy) -> None:
        engine = _env(tmp_path, _BAD_SKILL_MD)

        cli_findings = _serialize(engine.gate_of("demo"))     # CLI check 路径
        report = engine.push_skill("demo")                    # push 前置路径
        assert report.ok is False
        assert report.outcomes[0].error_code == "GATE_FAILED"

        assert len(spy) == 2, f"预期两条路径各跑一次 run_gate，实际 {len(spy)} 次"
        assert spy[0] == cli_findings
        assert spy[1] == cli_findings, "check 与 push 的 findings 出现分叉"

    def test_error_envelope_carries_rule_id(self, tmp_path, spy) -> None:
        """E-03.4 GWT：拦截时错误信封里要能读到具体规则 ID（下钻 / 排查都用它）。"""
        engine = _env(tmp_path, _BAD_SKILL_MD)
        report = engine.push_skill("demo")
        detail = report.outcomes[0].detail or ""
        assert "fm.description.required" in detail

    def test_dependencies_reach_the_gate(self, tmp_path, spy) -> None:
        """依赖规则真的会被真实快照触发（不是只有单测里能点亮的死规则）。"""
        engine = _env(tmp_path, _MISSING_DEP_MD)
        rules = [f["rule"] for f in _serialize(engine.gate_of("demo"))]
        assert "dep.missing" in rules
