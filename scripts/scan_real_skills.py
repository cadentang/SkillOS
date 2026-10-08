"""一次性脚本：扫描本机四个 Runtime 目录，落一份真实数据快照（03 §2 P0 产出）。

用途：后续开发与 demo 的数据基础（docs/skill-snapshot.json + 清单 md）。
只读操作，不写任何 runtime 目录。

用法：python scripts/scan_real_skills.py [--home PATH]
"""

from __future__ import annotations

import json
import sys
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skillos.core.algorithm.adapters.agents import AgentsAdapter  # noqa: E402
from skillos.core.algorithm.adapters.claude import ClaudeAdapter  # noqa: E402
from skillos.core.algorithm.adapters.codex import CodexAdapter  # noqa: E402
from skillos.core.algorithm.adapters.workbuddy import WorkBuddyAdapter  # noqa: E402
from skillos.core.algorithm.hash import is_excluded  # noqa: E402
from skillos.core.algorithm.parse import parse_frontmatter  # noqa: E402

# FR-1.0.1（06 A2）：SKILL.md 是唯一 canonical 标记；skill.md 作 legacy 兼容；
# README.md / CLAUDE.md 绝不算 —— 父目录一旦误判，其下的真 Skill 会被短路跳过。
_SKILL_MARKERS = ("SKILL.md", "skill.md")


def _find_skill_dirs(root: Path) -> list[Path]:
    """递归找含 SKILL.md 的包根（排除集与引擎统一）。父目录命中即不再下钻。"""
    found: list[Path] = []
    if not root.exists():
        return found
    for path in sorted(root.rglob("*")):
        if not path.is_dir() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if is_excluded(rel):
            continue
        if any((path / m).is_file() for m in _SKILL_MARKERS):
            found.append(path)
    # 去掉嵌套在其它包根下的目录（父目录命中即短路）
    result: list[Path] = []
    for d in found:
        if not any(d != p and d.is_relative_to(p) for p in found):
            result.append(d)
    return result


def _scan_runtime(adapter, home: Path) -> list[dict]:
    out: list[dict] = []
    roots = adapter.scan_roots("user", home=home)
    seen_roots: set[Path] = set()
    for root in roots:
        canon = root.resolve()
        if canon in seen_roots:  # 07 N9：软链同源根合并，防重复扫
            continue
        seen_roots.add(canon)
        for skill_dir in _find_skill_dirs(canon):
            marker = "SKILL.md" if (skill_dir / "SKILL.md").is_file() else "skill.md"
            text = (skill_dir / marker).read_text(encoding="utf-8", errors="replace")
            fm, _body = parse_frontmatter(text)
            out.append(
                {
                    "runtime": adapter.key,
                    "root": str(canon),
                    "dir_name": skill_dir.name,
                    "marker": marker,
                    "name": fm.get("name", ""),
                    "description": str(fm.get("description", ""))[:80],
                    "version": fm.get("version"),
                    "origin": fm.get("origin"),
                    "is_nested": skill_dir.parent.name == "subskills",
                }
            )
    return out


def main() -> int:
    home = Path(sys.argv[sys.argv.index("--home") + 1]) if "--home" in sys.argv else Path.home()
    adapters = [ClaudeAdapter(), AgentsAdapter(), WorkBuddyAdapter(), CodexAdapter()]

    entries: list[dict] = []
    for a in adapters:
        entries.extend(_scan_runtime(a, home))

    # 重复识别（FR-1.0.21 预研数据）：按 NFC 归一化的 dir/name 计数
    def _key(e: dict) -> str:
        return unicodedata.normalize("NFC", e["dir_name"]).casefold()

    counter = Counter(_key(e) for e in entries)
    dupes = {k: c for k, c in counter.items() if c > 1}

    snapshot = {
        "generated_by": "scripts/scan_real_skills.py",
        "home": str(home),
        "total_skills": len(entries),
        "per_runtime": dict(Counter(e["runtime"] for e in entries)),
        "duplicates": {k: c for k, c in sorted(dupes.items())},
        "entries": sorted(entries, key=lambda e: (e["runtime"], _key(e))),
    }

    docs = Path(__file__).resolve().parents[1] / "docs"
    out_json = docs / "skill-snapshot.json"
    out_json.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"总计 {len(entries)} 个 Skill")
    for rt, n in snapshot["per_runtime"].items():
        print(f"  {rt}: {n}")
    print(f"重复(同名多副本): {len(dupes)} 组")
    print(f"快照已写入 {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
