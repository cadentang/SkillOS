"""协作元数据随仓库版本化（04 §2.3 / FR-1.0.30 / 06 A8）。

事实分两份：
- 协作元数据（标签 / Profile 归属 / 启用状态）→ <repo>/.skillos/，进 git、随 push 同步
- 索引与判定缓存 → 本地 SQLite，不权威、可随时重建

两个 .skillos 不是同一个，别混：
- <repo>/.skillos/   仓库内元数据目录（进 git、会同步、被 scan 排除）
- ~/.skillos/        本机状态目录（锁 / 备份 / 日志 / SQLite，不进 git）

重建守卫：rebuild_index() 前必须过 rebuild_guard() —— 元数据说 0 个 Skill
而仓库里确有 Skill 目录时抛 META_INDEX_MISMATCH（类型化错误），拒绝重建。
否则一次失误（元数据损坏 / 首次克隆）会"重建"出空库再写回，清空整库。
首次克隆不走 rebuild_index()，走全量扫描（scan）重建元数据。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

META_DIR = ".skillos"
META_FILE = "meta.json"
META_FORMAT_VERSION = 1


class MetaIndexMismatch(Exception):
    """重建守卫拒绝（FR-1.0.30）：元数据说 0 个 Skill 而仓库里有 Skill 目录。

    类型化错误：启动流程只对这一种情况自愈（重新全量扫描），
    不把所有异常都当"重建一下就好了"。
    """

    code = "META_INDEX_MISMATCH"


@dataclass(frozen=True)
class SkillMetadata:
    """单 Skill 的协作元数据（仓库内、随 git 同步）。"""

    slug: str
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    profile: str | None = None  # Profile 归属


class MetaIndex:
    """<repo>/.skillos/meta.json 的读写（原子写 + 确定性序列化）。"""

    def __init__(self, repo_root: Path) -> None:
        # T1：repo_root 由调用方显式传入
        self.repo_root = repo_root
        self.meta_path = repo_root / META_DIR / META_FILE

    def read(self) -> dict[str, SkillMetadata]:
        """读全部元数据。文件不存在 → 空 dict（首次）；损坏 → 显式抛错，不静默兜底。"""
        if not self.meta_path.is_file():
            return {}
        raw = json.loads(self.meta_path.read_text(encoding="utf-8"))
        skills = raw.get("skills", {})
        out: dict[str, SkillMetadata] = {}
        for slug, item in skills.items():
            out[slug] = SkillMetadata(
                slug=slug,
                tags=list(item.get("tags", [])),
                enabled=bool(item.get("enabled", True)),
                profile=item.get("profile"),
            )
        return out

    def write(self, entries: dict[str, SkillMetadata]) -> None:
        """原子写（tmp + rename），键排序保证字节确定性 —— git diff 干净。"""
        payload = {
            "version": META_FORMAT_VERSION,
            "skills": {
                slug: {
                    "tags": sorted(entries[slug].tags),
                    "enabled": entries[slug].enabled,
                    "profile": entries[slug].profile,
                }
                for slug in sorted(entries)
            },
        }
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.meta_path)

    def skill_count(self) -> int:
        return len(self.read())


def rebuild_guard(meta_count: int, repo_skill_dirs: int) -> None:
    """重建守卫：元数据 0 个而仓库有 Skill 目录 → 拒绝（META_INDEX_MISMATCH）。

    规格（06 A8）只拒绝这一种组合；元数据损坏/缺失的恢复路径是全量扫描，
    不是 rebuild_index()。其余不一致放行，由重建过程以仓库为准修正。
    """
    if meta_count == 0 and repo_skill_dirs > 0:
        raise MetaIndexMismatch(
            f"metadata claims 0 skills but repo has {repo_skill_dirs} skill dirs; "
            "refusing to rebuild (run full scan instead)"
        )
