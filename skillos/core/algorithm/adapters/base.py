"""RuntimeAdapter 抽象（04 §2.1.2）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from skillos.core.model import SkillMeta, SkillSnapshot

# canonical frontmatter 字段 → keep | drop（由本机 62 个真实 Skill 实测得出，04 §2.1.2）
FieldPolicy = dict[str, str]

# frontmatter 输出时 canonical 字段名（allowed-tools 在 YAML 里是带连字符的键）
CANONICAL_FM_KEYS = ("name", "description", "allowed-tools", "version", "agent_created", "origin")


class RuntimeAdapter(ABC):
    """canonical → 目标 Runtime 的转换器。"""

    key: str                       # claude / agents / workbuddy / codex
    version: str                   # 适配器自身版本，参与 base 重建（FR-1.0.18）
    supports: frozenset[str]       # {"scripts","references","assets","subskills","sidecar"}
    field_policy: FieldPolicy      # canonical 字段 → "keep" | "drop"

    # ---- 目录解析 ----
    @abstractmethod
    def resolve_root(self, scope: str, project_dir: Path | None) -> Path:
        """返回该 Runtime 下 skill 目录的根路径。root 由调用方注入（T1），不读环境。"""

    # ---- frontmatter 转换 ----
    def frontmatter_out(self, meta: SkillMeta) -> dict:
        """canonical meta → 目标 Runtime 的 frontmatter。

        按 field_policy 逐字段 keep/drop；extra 字段透传（OUT-9）。
        纯函数：不含时间戳 / 随机值。
        """
        canonical: dict = {
            "name": meta.name,
            "description": meta.description,
            "allowed-tools": meta.allowed_tools,
            "version": meta.version,
            "agent_created": meta.agent_created,
            "origin": meta.origin,
        }
        out: dict = {}
        for k, v in canonical.items():
            policy = self.field_policy.get(k, "keep")
            if v is None:
                continue  # canonical 里本就没有的字段不输出
            if policy == "drop":
                continue
            out[k] = v
        # 未识别字段原样透传（OUT-9：不做语义转译）
        out.update(meta.extra)
        # name/description 是所有 runtime 的必留项
        out["name"] = meta.name
        out["description"] = meta.description
        return out

    @abstractmethod
    def frontmatter_in(self, raw: dict) -> SkillMeta:
        """目标 Runtime 的 frontmatter → canonical meta（pull 回收方向）。"""

    # ---- sidecar ----
    def sidecar_specs(self, meta: SkillMeta) -> dict[str, str]:
        """该 Runtime 需要的 sidecar 文件：相对路径 → 内容模板（纯函数）。"""
        return {}

    # ---- 渲染 ----
    @abstractmethod
    def render_layout(self, snapshot: SkillSnapshot) -> dict[str, bytes]:
        """canonical 快照 → {相对路径: 字节}。

        必须是纯函数（T6/T7）：同输入逐字节相同。此性质有单测守护
        （tests/test_adapters.py），违反则历史 base 会静默算错。
        """
