"""parse_frontmatter 单测（04 §2.1.1：缺失 / 非法一律降级为 ({}, text)，不抛异常）。"""

from __future__ import annotations

from skillos.core.algorithm.parse import parse_frontmatter


def test_ok() -> None:
    text = "---\nname: code-review\ndescription: 审查代码\n---\n\n正文内容"
    fm, body = parse_frontmatter(text)
    assert fm["name"] == "code-review"
    assert fm["description"] == "审查代码"
    assert body.strip() == "正文内容"


def test_missing_frontmatter() -> None:
    text = "# 只有正文"
    fm, body = parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_unclosed_frontmatter() -> None:
    text = "---\nname: x\n"
    fm, body = parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_invalid_yaml_degrades() -> None:
    # 非法 YAML（重复键嵌套错误 / 制表符缩进）必须降级，不得抛异常
    text = "---\nname: [unclosed\n  - bad: :\n---\nbody"
    fm, body = parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_frontmatter_not_dict() -> None:
    text = "---\n- just\n- a list\n---\nbody"
    fm, body = parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_first_line_not_delimiter() -> None:
    text = "----\nname: x\n---\nbody"
    fm, _ = parse_frontmatter(text)
    assert fm == {}
