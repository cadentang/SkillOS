"""解析器（04 §2.1.1，FR-1.0.2）。

规则：
- frontmatter 必须以 `---` 起止；解析失败降级为 `({}, text)`，不抛异常
  （防止单个坏文件阻断全量扫描，见 04 §6.1 错误分级）。
- 未识别字段进 meta.extra 并在分发时透传（OUT-9）。
"""

from __future__ import annotations

from typing import Any

import yaml

from skillos.core.model import FileEntry, SkillFile, SkillMeta

_FRONTMATTER_DELIM = "---"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """返回 (frontmatter dict, 正文)。frontmatter 缺失或非法时返回 ({}, text)。"""
    if not text.startswith(_FRONTMATTER_DELIM):
        return {}, text
    lines = text.splitlines()
    # 首行必须是独立的 ---（不匹配 ---- 或 ---abc）
    if lines[0].strip() != _FRONTMATTER_DELIM:
        return {}, text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == _FRONTMATTER_DELIM:
            raw = "\n".join(lines[1:idx])
            body = "\n".join(lines[idx + 1 :])
            try:
                data = yaml.safe_load(raw)
            except yaml.YAMLError:
                # T: 降级不抛异常 —— 单个坏文件不得阻断全量扫描
                return {}, text
            if not isinstance(data, dict):
                return {}, text
            return data, body
    # 只有起始 --- 没有闭合
    return {}, text


_CANONICAL_FIELDS = frozenset(
    {"name", "description", "version", "agent_created", "origin", "allowed-tools"}
)


def _build_meta(fm: dict[str, Any]) -> SkillMeta | None:
    """从 frontmatter dict 构造 SkillMeta；缺 name/description 的仍可解析（由 checks 报 error）。"""
    if not isinstance(fm.get("name"), str) or not isinstance(fm.get("description"), str):
        # 结构非法：交给 checks 层报 fm.name.required / fm.description.required
        return None
    return SkillMeta(
        name=fm["name"],
        description=fm["description"],
        version=fm["version"] if isinstance(fm.get("version"), str) else None,
        agent_created=fm["agent_created"] if isinstance(fm.get("agent_created"), bool) else None,
        origin=fm["origin"] if isinstance(fm.get("origin"), str) else None,
        allowed_tools=fm["allowed-tools"] if isinstance(fm.get("allowed-tools"), list) else None,
        extra={k: v for k, v in fm.items() if k not in _CANONICAL_FIELDS},
    )


def parse_skill(
    skill_md: str,
    files: list[FileEntry],
    sidecars: dict[str, str],
) -> tuple[SkillMeta | None, list[SkillFile]]:
    """解析 SKILL.md + 文件清单 + sidecar 文本 → (SkillMeta, [SkillFile])。

    sidecars: 相对路径 → 文本内容（如 agents/openai.yaml），未识别字段原样保留。
    """
    fm, _body = parse_frontmatter(skill_md)
    meta = _build_meta(fm) if fm else None

    out: list[SkillFile] = []
    for f in files:
        entry_meta = meta if f.path == "SKILL.md" else None
        out.append(SkillFile(path=f.path, meta=entry_meta, is_text=f.is_text))
    for path, _content in sidecars.items():
        out.append(SkillFile(path=path, meta=None, is_text=True))
    return meta, out
