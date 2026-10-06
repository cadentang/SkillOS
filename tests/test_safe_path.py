"""safe_path 单测（04 §2.2.0 六条约束逐项验证）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from skillos.core.fs.safe_path import PathTraversalError, safe_path


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "skills"


def test_ok_nested(root: Path) -> None:
    target = safe_path(root, "code-review/references/x.md")
    assert target == (root / "code-review" / "references" / "x.md").resolve()


def test_absolute_rejected(root: Path) -> None:
    with pytest.raises(PathTraversalError):
        safe_path(root, str(Path("C:/Windows/system32/config").resolve() if Path("C:/").exists() else "/etc/passwd"))


def test_parent_escape_rejected(root: Path) -> None:
    with pytest.raises(PathTraversalError):
        safe_path(root, "../../etc/passwd")


def test_windows_reserved_name(root: Path) -> None:
    with pytest.raises(PathTraversalError):
        safe_path(root, "CON.md")
    with pytest.raises(PathTraversalError):
        safe_path(root, "sub/COM1.txt")


def test_illegal_chars_rejected(root: Path) -> None:
    with pytest.raises(PathTraversalError):
        safe_path(root, "a<b.md")
    with pytest.raises(PathTraversalError):
        safe_path(root, 'a"b.md')


def test_empty_rejected(root: Path) -> None:
    with pytest.raises(PathTraversalError):
        safe_path(root, "")
    with pytest.raises(PathTraversalError):
        safe_path(root, "   ")


def test_error_message_no_abs_path(root: Path) -> None:
    """约束 5：错误信息不回显解析后的绝对路径。"""
    with pytest.raises(PathTraversalError) as ei:
        safe_path(root, "../secret.txt")
    assert str(root.resolve()) not in str(ei.value)
