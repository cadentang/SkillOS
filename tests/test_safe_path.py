"""safe_path 单测（04 §2.2.0 六条约束逐项验证）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from skillos.core.fs.safe_path import PathTraversalError, path_key, safe_path


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "skills"


def test_ok_nested(root: Path) -> None:
    target = safe_path(root, "code-review/references/x.md")
    assert target == (root / "code-review" / "references" / "x.md").resolve()


def test_absolute_rejected(root: Path) -> None:
    abs_path = str(
        Path("C:/Windows/system32/config").resolve() if Path("C:/").exists() else "/etc/passwd"
    )
    with pytest.raises(PathTraversalError):
        safe_path(root, abs_path)


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


class TestPathKey:
    """路径键归一化（FR-1.0.31 / 04 §2.2.0-10 / 06 A7）。

    坑：macOS 写盘即 NFD 化、Windows 大小写不敏感 —— 只比大小写会漏掉
    中文名 / 组合字符场景（本机 62 个 Skill 全中文名）。不修则同一路径
    被判成两个，冲突检测失效。
    """

    def test_casefold(self):
        assert path_key("Code-Review/SKILL.md") == path_key("code-review/SKILL.md")

    def test_nfc_folds_decomposed(self):
        composed = "café/SKILL.md"
        decomposed = "cafe\u0301/SKILL.md"
        assert path_key(composed) == path_key(decomposed)

    def test_separator_normalized(self):
        assert path_key("a\\b\\c.md") == path_key("a/b/c.md")

    def test_distinct_paths_stay_distinct(self):
        assert path_key("a/SKILL.md") != path_key("b/SKILL.md")

    def test_key_is_not_real_path(self):
        """path_key 只用于比较，不得替换用户可见的真实路径。"""
        real = "Code-Review/SKILL.md"
        assert path_key(real) != real
