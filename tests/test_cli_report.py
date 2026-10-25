"""CLI `skillos report [--last]`（N-02.1 无 daemon 时的降级路径）。

只验证三件事：命令存在、无报告时 `--last` 明确报错、空库也能生成一份基线。
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from skillos.cli.main import EXIT_OK, EXIT_USAGE, app


def _invoke(tmp_path: Path, monkeypatch, args: list[str]):
    """把 home 与 repo 都指到临时目录 —— 绝不触碰真实 ~/.skillos。"""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("SKILLOS_ROOT", str(home))
    repo = tmp_path / "repo"
    return CliRunner().invoke(app, [*args, "--repo", str(repo)])


def test_report_command_registered() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == EXIT_OK
    assert "report" in result.stdout


def test_last_without_reports_exits_with_usage_code(tmp_path, monkeypatch) -> None:
    """没有报告时不能假装成功 —— 退出码要能区分"还没生成"与"生成失败"。"""
    result = _invoke(tmp_path, monkeypatch, ["report", "--last"])
    assert result.exit_code == EXIT_USAGE
    assert "还没有报告" in result.stdout


def test_first_report_is_a_baseline(tmp_path, monkeypatch) -> None:
    # 与真实使用顺序一致：先 init（建仓库 + 库），再 report
    created = _invoke(tmp_path, monkeypatch, ["init"])
    assert created.exit_code == EXIT_OK, created.stdout

    result = _invoke(tmp_path, monkeypatch, ["report"])
    assert result.exit_code == EXIT_OK, result.stdout
    assert "体检报告" in result.stdout
    # 空仓库（还没提交）没有漂移可言 —— 报告照样生成，但必须说明为什么是 0
    assert "无变化" in result.stdout

    again = _invoke(tmp_path, monkeypatch, ["report", "--last"])
    assert again.exit_code == EXIT_OK
    assert "体检报告" in again.stdout
