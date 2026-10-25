"""扫描器（04 §2.2.1，FR-1.0.1 / FR-1.0.3）。

包边界规则：
1. 含 SKILL.md（大写）的目录即包根 —— 唯一 canonical 标记；skill.md（全小写）
   作 legacy 兼容识别；README.md / CLAUDE.md 绝不算标记（06 A2：父目录一旦
   误判，其下的真 Skill 会被短路跳过）。
2. 包根下 subskills/<name>/SKILL.md 识别为子技能（is_nested=True），计入清单。
3. 跳过集由 hash.is_excluded() 统一给出（__pycache__ / .git / .skillos /
   node_modules / .runtime / .venv / *.pyc / .DS_Store / 噪音），不在此另立。
4. 符号链接不跟随（防环）。
5. 扫描根按 canonical path 合并去重（07 N9：软链同源根只扫一次）。

root 由调用方显式传入（T1）；扫描只发生在调用方列出的根内（FR-1.0.25）。
"""

from __future__ import annotations

import os
from pathlib import Path

from skillos.core.algorithm.hash import aggregate_entries, is_excluded, list_content_files
from skillos.core.algorithm.parse import build_meta, parse_frontmatter
from skillos.core.model import RootSpec, ScannedSkill

# FR-1.0.1（06 A2）：SKILL.md 唯一 canonical 标记；skill.md legacy；README/CLAUDE 绝不算
_CANONICAL_MARKER = "SKILL.md"
_LEGACY_MARKER = "skill.md"

_SUBSKILLS_DIR = "subskills"


def _marker_of(d: Path) -> str | None:
    if (d / _CANONICAL_MARKER).is_file():
        return _CANONICAL_MARKER
    if (d / _LEGACY_MARKER).is_file():
        return _LEGACY_MARKER
    return None


def _parse_meta(marker_path: Path):
    try:
        text = marker_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fm, _body = parse_frontmatter(text)
    return build_meta(fm) if fm else None


def _make_scanned(root: Path, skill_dir: Path, spec: RootSpec, is_nested: bool) -> ScannedSkill:
    marker = _marker_of(skill_dir) or _CANONICAL_MARKER
    meta = _parse_meta(skill_dir / marker)
    entries = list_content_files(skill_dir)
    return ScannedSkill(
        root=root,
        rel_path=skill_dir.relative_to(root).as_posix(),
        runtime_key=spec.runtime_key,
        scope=spec.scope,
        meta=meta,
        pack_hash=aggregate_entries(entries),
        is_nested=is_nested,
    )


def scan_root(root: Path, spec: RootSpec, ignore: frozenset[str]) -> list[ScannedSkill]:
    """单个根目录扫描：目录树遍历 + SKILL.md 包边界识别 + pack_hash。

    单个目录解析失败降级继续（记 warning 到返回外），不中断全量扫描（04 §6.1）。
    """
    root = root.resolve()
    if not root.is_dir():
        return []
    out: list[ScannedSkill] = []
    # 防环兜底（镜像 xingkongliang/skills-manager collect_skill_dirs_recursive
    # `visited: HashSet<PathBuf>`）：canonicalize 后的去重集。同物理目录被多
    # 条路径访问时不重复枚举 —— 根本身是 symlink、或跨平台 resolve 行为差异
    # 的最后兜底（os.walk(followlinks=False) + is_symlink 二次过滤已覆盖大
    # 部分情况）。resolve 失败时回退原路径，跟对方一致不抛错。
    seen: set[Path] = set()
    # os.walk(followlinks=False)：不进符号链接目录（防环）；先序确保父目录先命中
    for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
        cur = Path(dirpath)
        try:
            canon = cur.resolve()
        except OSError:
            canon = cur
        if canon in seen:
            dirnames[:] = []  # 已访问过的物理目录：不下钻
            continue
        seen.add(canon)
        # 过滤：排除集 + 调用方附加 ignore + 符号链接目录（不跟随）
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in ignore
            and not (cur / d).is_symlink()
            and not is_excluded((cur / d).relative_to(root).as_posix())
        )
        rel = cur.relative_to(root).as_posix()
        if rel != "." and is_excluded(rel):
            continue
        # 扫描根是 Runtime 的**容器目录**，永不作为包根：根上若被误放一个 SKILL.md，
        # 按包边界规则会命中"包根之下不再寻找其它包根"，整个 Runtime 被短路。
        # 实测代价：`~/.claude/skills/SKILL.md` 让 claude 端 42 个真 Skill 集体消失
        # （2026-10-25）。容器目录没有"自己是一个 Skill"的语义。
        if rel == ".":
            continue
        marker = _marker_of(cur)
        if marker is None:
            continue  # 不是包根：继续下钻（README 不短路，06 A2）
        # 包根命中：产出 + 识别 subskills 嵌套，其余子目录不再独立识别
        out.append(_make_scanned(root, cur, spec, is_nested=False))
        sub_root = cur / _SUBSKILLS_DIR
        if sub_root.is_dir():
            for sub in sorted(sub_root.iterdir()):
                if sub.is_dir() and not sub.is_symlink() and _marker_of(sub):
                    out.append(_make_scanned(root, sub, spec, is_nested=True))
        dirnames[:] = [d for d in dirnames if d != _SUBSKILLS_DIR]
        dirnames[:] = []  # 包根之下不再寻找其它包根（包边界短路）
    return out


def scan_roots(roots: list[RootSpec], ignore: list[str] | None = None) -> list[ScannedSkill]:
    """扫描多个 Runtime 根，返回全部识别到的 Skill 包。

    扫描根按 canonical path（resolve）去重（07 N9）—— 两个 Runtime 根指向
    同一物理目录时只产出一份，避免假 UNMANAGED 与重复纳管。
    """
    ignore_set = frozenset(ignore or ())
    seen: set[Path] = set()
    out: list[ScannedSkill] = []
    for spec in roots:
        canon = spec.root.resolve()  # 合并软链指向同一物理目录的根
        if canon in seen:
            continue
        seen.add(canon)
        out.extend(scan_root(spec.root, spec, ignore_set))
    return out
