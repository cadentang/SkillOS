"""生成沙盒预置演示数据（P7 / FR-3.10）。

用法：python scripts/build_sandbox_seed.py <seed_dir> [--runtime-root NAME ...]

产出 <seed_dir>/runtime/<runtime_key>/<slug>/…（reset 与首次安装的恢复源）。

Skill 集合对齐 demo 剧本（docs/03 §7 双高潮）：
- code-review   中文描述（中文 skill 全链路 + 时间轴作者叙事）
- api-design    带 references/（文件树 + 多文件分发演示）
- daily-report  常规 skill（批量分发基数）

约束：seed 集合不得含 error 级门禁违规 —— sandbox_reset 会对全部 skill
执行 push，门禁 error 会让重置失败（写操作不猜用户意图）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

# 四端 deploy 根名（adapters/ 数据驱动，脚本只按数据复制）
_DEFAULT_RUNTIME_KEYS = ("claude", "agents", "workbuddy", "codex")

_SKILLS: dict[str, tuple[bytes, dict[str, bytes]]] = {
    "code-review": (
        (
            "---\n"
            "name: code-review\n"
            "description: 对指定代码变更做结构化评审，按严重程度分级输出问题清单与改进建议\n"
            "---\n"
            "\n"
            "# 代码评审\n\n"
            "按正确性、可读性、边界条件三类逐项检查，输出 P0/P1/P2 分级清单。\n"
        ).encode("utf-8"),
        {},
    ),
    "api-design": (
        (
            "---\n"
            "name: api-design\n"
            "description: 依据 REST 约定设计资源模型与接口契约，输出 OpenAPI 风格草稿\n"
            "---\n"
            "\n"
            "# API 设计\n\n"
            "资源命名、状态码、分页与错误结构约定见 references。\n"
        ).encode("utf-8"),
        {
            "references/conventions.md": (
                "# 接口约定\n\n- 资源名用复数名词\n- 错误体统一 {code, message, detail}\n"
            ).encode("utf-8"),
        },
    ),
    "daily-report": (
        (
            "---\n"
            "name: daily-report\n"
            "description: 汇总当日工作记录生成日报草稿，按项目分组列出进展与风险\n"
            "---\n"
            "\n"
            "# 日报生成\n\n"
            "输入零散工作记录，输出去重后的分组日报。\n"
        ).encode("utf-8"),
        {},
    ),
}


def build(seed_dir: Path, runtime_keys: list[str]) -> int:
    runtime = seed_dir / "runtime"
    count = 0
    for key in runtime_keys:
        for slug, (skill_md, extras) in _SKILLS.items():
            d = runtime / key / slug
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_bytes(skill_md)
            for rel, content in extras.items():
                p = d / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(content)
            count += 1
    return count


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("seed_dir", type=Path)
    ap.add_argument("--runtime-key", action="append", default=None,
                    help="runtime deploy 根名（默认四端全量）")
    args = ap.parse_args()
    keys = args.runtime_key or list(_DEFAULT_RUNTIME_KEYS)
    n = build(args.seed_dir, keys)
    print(f"seed 就绪: {args.seed_dir / 'runtime'}（{len(keys)} 端 × {n // len(keys)} skill）")


if __name__ == "__main__":
    main()
