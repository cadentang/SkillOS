"""门禁豁免开关的「表面」红线（E-03.5 / NFR-10.2）。

`--allow-gate-failure` 只允许存在于 CLI 人工路径。MCP / WS / API / daemon
不得出现等价开关 —— 一旦出现，Agent 就能自己绕过质量门禁，治理叙事失效
（CODEBUDDY.md 红线 13：MCP 写工具不暴露 force）。

做法是静态扫描 surface，而不是靠人眼：新增一个工具/op 时本测试会立刻发现。
"""

from __future__ import annotations

import ast
from pathlib import Path

from skillos.core.constants import WS_TASK_OPS

SKILLOS_DIR = Path(__file__).resolve().parents[1] / "skillos"
MCP_SERVER = SKILLOS_DIR / "mcp" / "server.py"
DAEMON_MAIN = SKILLOS_DIR / "daemon" / "main.py"
CLI_MAIN = SKILLOS_DIR / "cli" / "main.py"

# 参数名里出现这些词 = 可能在开豁免口子
_BYPASS_HINTS = ("force", "allow_gate", "skip_gate", "no_gate", "gate_bypass")


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _is_tool_decorator(node: ast.AST) -> bool:
    """`@mcp.tool()` 或 `@tool`。"""
    if isinstance(node, ast.Call):
        node = node.func
    return isinstance(node, ast.Attribute) and node.attr == "tool"


def _mcp_tools() -> list[tuple[str, list[str]]]:
    tree = ast.parse(MCP_SERVER.read_text(encoding="utf-8"))
    tools: list[tuple[str, list[str]]] = []
    for fn in _functions(tree):
        if any(_is_tool_decorator(d) for d in fn.decorator_list):
            args = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
            tools.append((fn.name, args))
    return tools


class TestMcpSurface:
    def test_mcp_tools_are_discovered(self) -> None:
        """先确认扫描本身有效：一个工具都没扫到说明测试成了空转。"""
        assert len(_mcp_tools()) >= 5

    def test_no_gate_bypass_parameter_in_mcp_tools(self) -> None:
        offenders = [
            f"{name}({', '.join(args)})"
            for name, args in _mcp_tools()
            if any(hint in arg.lower() for arg in args for hint in _BYPASS_HINTS)
        ]
        assert not offenders, f"MCP 工具暴露了门禁豁免参数：{offenders}"

    def test_mcp_write_tools_default_to_dry_run(self) -> None:
        """写工具必须带 `dry_run` 且默认 True（CODEBUDDY.md 红线 13）。"""
        write_tools = [(n, a) for n, a in _mcp_tools() if any(
            verb in n for verb in ("push", "pull", "upgrade", "sync")
        )]
        assert write_tools, "没扫到写工具，扫描口径可能失效"
        tree = ast.parse(MCP_SERVER.read_text(encoding="utf-8"))
        defaults: dict[str, bool] = {}
        for fn in _functions(tree):
            if not any(_is_tool_decorator(d) for d in fn.decorator_list):
                continue
            args = fn.args.args
            paired = list(zip(
                args[len(args) - len(fn.args.defaults):], fn.args.defaults, strict=True,
            ))
            for arg, default in paired:
                if arg.arg == "dry_run":
                    defaults[fn.name] = (
                        isinstance(default, ast.Constant) and default.value is True
                    )
        for name, _ in write_tools:
            assert defaults.get(name) is True, f"{name} 的 dry_run 默认值不是 True"


class TestGateBypassIsCliOnly:
    def test_allow_gate_failure_only_passed_as_literal_false(self) -> None:
        """MCP / daemon 里只允许 `allow_gate_failure=False` 这种常量写法。"""
        for path in (MCP_SERVER, DAEMON_MAIN):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg != "allow_gate_failure":
                        continue
                    assert isinstance(kw.value, ast.Constant) and kw.value.value is False, (
                        f"{path.name}: allow_gate_failure 不得由参数/变量控制（{node.lineno}）"
                    )

    def test_cli_is_the_only_place_declaring_the_option(self) -> None:
        """只认「真的声明了这个 CLI 选项」，注释里提到不算（checks.py 的说明文字会提及它）。"""
        declaring: list[str] = []
        for path in SKILLOS_DIR.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name != "Option":
                    continue
                if any(
                    isinstance(a, ast.Constant) and a.value == "--allow-gate-failure"
                    for a in node.args
                ):
                    declaring.append(path.relative_to(SKILLOS_DIR).as_posix())
        assert declaring == ["cli/main.py"], declaring

    def test_ws_task_ops_carry_no_bypass_op(self) -> None:
        offenders = [
            op for op in WS_TASK_OPS
            if any(hint in op.lower() for hint in _BYPASS_HINTS)
        ]
        assert not offenders, f"WS 任务白名单出现门禁豁免 op：{offenders}"
