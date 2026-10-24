"""T11 红线守护（E-01.5）：健康度只能用于展示 / 排序，不得参与放行判定。

`docs/11` E-01.5 要求「静态搜索 `health_score` 全部读取点，必须全在展示/排序路径
（禁止 `if health_score < X: block`）」。靠人眼 review 守不住这条，所以做成测试：

1. 读取点必须落在白名单模块（计算点 / 序列化 / 数据准备），
2. 任何读取点都不得出现在比较、条件分支（if / while / 三元）里，
3. 分数公式只有一个计算点（`summarize_health`），别处不得再算一遍。
"""

from __future__ import annotations

import ast
from pathlib import Path

SKILLOS_DIR = Path(__file__).resolve().parents[1] / "skillos"

# 允许读健康度的位置：计算点 + API 序列化 + 数据准备（都不是放行判定）
ALLOWED_MODULES = {
    "core/algorithm/checks.py",
    "api/routes/checks.py",
    "services/engine.py",
}

# 条件分支节点：出现在其 test 里的读取点就是「拿分数当开关」
_BRANCH_NODES = (ast.If, ast.While, ast.IfExp)
_COMPARE_NODES = (ast.Compare,)


def _modules() -> list[Path]:
    return [
        p for p in SKILLOS_DIR.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def _rel(path: Path) -> str:
    return path.relative_to(SKILLOS_DIR).as_posix()


def _is_health_score(node: ast.AST) -> bool:
    """命中 `health_score` 名字 / 属性，或 `health["score"]` 形式的取数。"""
    if isinstance(node, ast.Name) and node.id == "health_score":
        return True
    if isinstance(node, ast.Attribute) and node.attr == "health_score":
        return True
    if isinstance(node, ast.Subscript):
        key = node.slice
        if isinstance(key, ast.Constant) and key.value == "score":
            base = node.value
            name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
            return "health" in name
    return False


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    table: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            table[child] = parent
    return table


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parent_map = _parents(tree)
    found: list[str] = []

    for node in ast.walk(tree):
        if not _is_health_score(node):
            continue
        cursor = parent_map.get(node)
        while cursor is not None:
            if isinstance(cursor, _COMPARE_NODES):
                found.append(f"{_rel(path)}:{node.lineno} 健康度参与比较")
                break
            if isinstance(cursor, _BRANCH_NODES) and node in list(ast.walk(cursor.test)):
                found.append(f"{_rel(path)}:{node.lineno} 健康度出现在条件分支")
                break
            cursor = parent_map.get(cursor)

    if _rel(path) not in ALLOWED_MODULES and any(
        _is_health_score(n) for n in ast.walk(tree)
    ):
        found.append(f"{_rel(path)} 不在健康度只读白名单内却读了健康度")
    return found


class TestT11Redline:
    def test_no_health_score_branching(self) -> None:
        violations: list[str] = []
        for path in _modules():
            violations.extend(_violations(path))
        assert not violations, "T11 红线违规：\n" + "\n".join(violations)

    def test_score_formula_has_single_computation_site(self) -> None:
        """分数公式只能出现在 summarize_health（否则又多一个「真相」）。"""
        owners = [
            _rel(p) for p in _modules()
            if "HEALTH_SCORE_MAX -" in p.read_text(encoding="utf-8")
        ]
        assert owners == ["core/algorithm/checks.py"], owners

    def test_health_has_no_gate_flag(self) -> None:
        """健康度结构里不得出现可当放行依据的字段（与 test_checks 同一条防线）。"""
        from skillos.core.algorithm.checks import summarize_health

        health = summarize_health([])
        for forbidden in ("has_error", "pass", "ok"):
            assert forbidden not in health
