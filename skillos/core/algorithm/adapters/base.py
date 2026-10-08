"""RuntimeAdapter 抽象（04 §2.1.2 / FR-1.0.8 / FR-1.0.25 / T6 / T7 / T12）。

路径即数据 + 渲染能力即数据：新增 Runtime 只加数据（RuntimePaths / field_policy /
supports），不改渲染器代码；adapters/ 各文件里不得出现字面量路径与 if key 分支。

纯函数红线（T6 / T7）：render_layout() 同一 SkillSnapshot + 同一 adapter.version
调用两次，输出逐字节相同 —— 禁止时间戳、随机数、环境变量、本机路径。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

from skillos.core.model import FileEntry, SkillMeta, SkillSnapshot

# canonical frontmatter 字段 → keep | drop（由本机 62 个真实 Skill 实测得出，04 §2.1.2）
FieldPolicy = dict[str, str]

# canonical frontmatter 字段名（allowed-tools 在 YAML 里是带连字符的键）
CANONICAL_FM_KEYS = ("name", "description", "allowed-tools", "version", "agent_created", "origin")

# 首段目录名 → supports 类别；未列出的首段目录（根级文件 / 未知目录）一律透传，不丢用户数据
_CATEGORY_SUPPORTS = {
    "scripts": "scripts",
    "references": "references",
    "assets": "assets",
    "subskills": "subskills",
    "agents": "sidecar",
}


# ---------------------------------------------------------------- 路径即数据（FR-1.0.25）


@dataclass(frozen=True)
class PathSpec:
    """一条路径事实。key 带命名空间（builtin: / custom:）实现用户覆盖的隔离（07 N3）。"""

    key: str          # "builtin:claude" / "custom:my-fork"
    scope: str        # "user" | "project"
    template: str     # 支持 {home} / {project} 占位符
    source: str       # "builtin" | "custom"

    def __post_init__(self) -> None:
        if self.scope == "project":
            # 06 B5：项目级路径必须相对且不得含 .. —— 绝对路径会写进别人机器的错误位置，
            # 含 .. 等于把部署目标放到项目之外
            pure = PureWindowsPath(self.template)
            if pure.is_absolute() or PurePosixPath(self.template).is_absolute():
                raise ValueError(f"project template must be relative: {self.template!r}")
            if ".." in pure.parts or ".." in PurePosixPath(self.template).parts:
                raise ValueError(f"project template must not contain '..': {self.template!r}")


@dataclass(frozen=True)
class RuntimePaths:
    """一个 Runtime 的路径事实。部署根与发现根分开建模 —— 二者相同是巧合，不是定义。"""

    deploy_dir: PathSpec             # 部署根：我们往这里写
    scan_only_dirs: list[PathSpec] = field(default_factory=list)   # 发现根：只扫、不写
    fallback_dirs: list[PathSpec] = field(default_factory=list)    # deploy 不存在时的回退链
    # 共用物理目录（停用 / 清理前须引用计数归零，07 N1/N5）
    shared_dirs: list[PathSpec] = field(default_factory=list)
    recursive_scan: bool = True      # 是否递归子目录（FR-1.0.8）
    verified_at: date | None = None  # 本机实测确认日期；为空 = 未验证，UI 必须出警告
    evidence: str = "local-observed"  # "official-doc" | "upstream-source" | "local-observed"


def render_spec(spec: PathSpec, home: Path, project_dir: Path | None) -> Path:
    """渲染 PathSpec 模板。T1：home / project 由调用方显式注入，不读环境。"""
    if spec.scope == "project":
        if project_dir is None:
            raise ValueError(f"project scope 需要 project_dir（{spec.key}）")
        return Path(spec.template.replace("{project}", str(project_dir)))
    return Path(spec.template.replace("{home}", str(home)))


class RuntimeAdapter(ABC):
    """canonical → 目标 Runtime 的转换器。

    key 用短名（与 store.Runtime.key 一致）；命名空间隔离（FR-1.0.25）落在
    PathSpec.key（builtin: / custom:），覆盖解析"先 custom 后 builtin"由注册表承担。
    """

    key: str                       # claude / agents / workbuddy / codex
    version: str                   # 适配器自身版本，参与 base 重建（FR-1.0.18）
    supports: frozenset[str]       # {"scripts","references","assets","subskills","sidecar"}
    field_policy: FieldPolicy      # canonical 字段 → "keep" | "drop"
    paths: dict[str, RuntimePaths]  # scope → 路径事实（数据，非代码）

    # ---- 目录解析 ----
    def resolve_root(self, scope: str, home: Path, project_dir: Path | None = None) -> Path:
        """按 deploy_dir → fallback_dirs 顺序解析部署根；都不存在返回 deploy_dir（首装），
        不回退到任何硬编码默认值（04 §2.1.2）。"""
        rp = self.paths[scope]
        deploy = render_spec(rp.deploy_dir, home, project_dir)
        if deploy.exists():
            return deploy
        for fb in rp.fallback_dirs:
            candidate = render_spec(fb, home, project_dir)
            if candidate.exists():
                return candidate
        return deploy

    def scan_roots(self, scope: str, home: Path, project_dir: Path | None = None) -> list[Path]:
        """发现集合：deploy_dir + scan_only_dirs + fallback_dirs（只扫这些，不猜）。"""
        rp = self.paths[scope]
        specs = [rp.deploy_dir, *rp.scan_only_dirs, *rp.fallback_dirs]
        return [render_spec(s, home, project_dir) for s in specs]

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
    def render_layout(self, snapshot: SkillSnapshot) -> dict[str, bytes]:
        """canonical 快照 → {相对路径: 字节}。通用实现，只依赖本类声明的数据。

        必须是纯函数（T6/T7）：同输入逐字节相同。此性质有单测守护
        （tests/test_adapters_layout.py），违反则历史 base 会静默算错。
        """
        out: dict[str, bytes] = {}
        for f in snapshot.files:
            if not f.in_git:
                continue  # T2：未纳管资产不分发
            if f.path == "SKILL.md":
                out["SKILL.md"] = self._render_skill_md(snapshot.meta, f)
                continue
            category = f.path.split("/", 1)[0] if "/" in f.path else None
            if category in _CATEGORY_SUPPORTS and _CATEGORY_SUPPORTS[category] not in self.supports:
                continue  # 该 Runtime 不支持此类别
            out[f.path] = f.content  # 字节级透传，不归一化（T16）
        if "sidecar" in self.supports:
            for rel, tmpl in self.sidecar_specs(snapshot.meta).items():
                out[rel] = tmpl.encode("utf-8")
        return out

    def _render_skill_md(self, meta: SkillMeta, f: FileEntry) -> bytes:
        """按 frontmatter_out 重建 SKILL.md，正文按字节保留。

        正文提取保持字节级：body 是第二个 `---` 行之后的全部内容（含尾随换行），
        不经 decode/encode 往返，避免任何字节变化。
        """
        body = _extract_body(f.content)
        if body is None:
            return f.content  # canonical 无 frontmatter：原样透传
        fm = yaml.safe_dump(
            self.frontmatter_out(meta),
            sort_keys=True,
            allow_unicode=True,
            default_flow_style=False,
        )
        return b"---\n" + fm.encode("utf-8") + b"---\n" + body


def _extract_body(content: bytes) -> bytes | None:
    """提取 frontmatter 之后的正文（字节级）。无合法 frontmatter 返回 None。"""
    lines = content.split(b"\n")
    if not lines or lines[0].strip() != b"---":
        return None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == b"---":
            return b"\n".join(lines[idx + 1 :])
    return None
