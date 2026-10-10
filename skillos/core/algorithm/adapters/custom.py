"""Custom Runtime 通用 adapter（FR-1.0.34 / T25 / FR-2.4.10）。

custom Runtime（如 custom:trae）无已知格式约定 —— 策略 = 最大兼容：
  - 全字段 keep（frontmatter 原样）
  - 全类别 supports（scripts/references/assets/subskills/sidecar 透传）
  - 无 sidecar（目标格式未知，不猜模板）
  - 渲染 = 字节级透传（canonical SKILL.md 原样 + 目录结构原样）

路径事实由云端 Runtime 表下发（runtime_sync_custom task），daemon 本地
SQLite runtimes 行是登记处；本 adapter 只做数据容器 —— root_path 是
用户机器的绝对路径，只影响 resolve_root，不参与 render_layout
（T6/T7 纯函数红线不受影响）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from skillos.core.algorithm.adapters.base import (
    PathSpec,
    RuntimeAdapter,
    RuntimePaths,
)
from skillos.core.model import SkillMeta


class CustomRuntimeAdapter(RuntimeAdapter):
    """key = "custom:<name>"，root_path 来自云端同步。"""

    def __init__(self, key: str, root_path: str) -> None:
        if not key.startswith("custom:"):
            raise ValueError(f"custom adapter key 必须以 custom: 开头: {key!r}")
        self.key = key
        self.version = "1"  # 适配器自身版本，参与 base 重建（FR-1.0.18）
        self.supports = frozenset(
            {"scripts", "references", "assets", "subskills", "sidecar"}
        )
        self.field_policy = {}  # 全 keep（默认）
        # root_path 是用户机绝对路径（user scope，非 project —— project
        # 校验会拒绝绝对路径）；template 无占位符 = 原样返回
        spec = PathSpec(
            key=f"custom:{key}",
            scope="user",
            template=str(root_path),
            source="custom",
        )
        self.paths = {
            "user": RuntimePaths(
                deploy_dir=spec,
                recursive_scan=True,
                verified_at=date.today(),  # 云端创建时用户已确认路径存在
                evidence="user-confirmed",
            ),
        }

    def resolve_root(self, scope: str, home: Path, project_dir: Path | None = None) -> Path:
        """custom 只支持 user scope；根 = 云端下发的 root_path 原样。"""
        if scope != "user":
            raise ValueError(f"custom Runtime 不支持 scope: {scope}")
        return Path(self.paths["user"].deploy_dir.template)

    def frontmatter_in(self, raw: dict) -> SkillMeta:
        """custom = canonical 超集透传，直接映射回收。"""
        return SkillMeta(
            name=raw.get("name") or "",
            description=raw.get("description") or "",
            allowed_tools=raw.get("allowed-tools"),
            version=raw.get("version"),
            agent_created=raw.get("agent_created"),
            origin=raw.get("origin"),
            extra={
                k: v for k, v in raw.items()
                if k not in {
                    "name", "description", "allowed-tools", "version",
                    "agent_created", "origin",
                }
            },
        )
