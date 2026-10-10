"""Runtime 约定俗成探测（FR-1.0.32 / T24 / D8 / FR-2.4.10）。

白名单 = 数据（builtin_potential_runtimes.yaml），本模块**零字面量
路径**（`~/.cursor` 等），新增 / 删除探测项只动 yaml。命中判据仅 =
该路径下存在至少一个 SKILL.md（沿用 list_content_files 单一入口，T16）。

签名门控（镜像 xingkongliang/skills-manager backfill_stranded_agent_targets
的 ``stranded_candidate_signature``，简化版）：

- signature = sha256(yaml 自身 bytes ‖ sorted([(key, root_path, path_exists)]))
- cache 文件 = ``home / ".skillos" / "discover_cache.json"``（与 daemon 锁同目录）
- 命中 → 直接返回 cache（零 list_content_files 调用）
- 不命中 → 跑全量、原子写 cache
- cache IO 失败 / JSON 损坏 → 降级全量，不抛错（best-effort 门控）

⚠️ 陈旧风险：用户在已有 path 下新增/删除 skill（不动 yaml、不动 path 存在性）
   时签名不变，返回的 skill_count 仍是上次的。NFR 性能 62 个 skill 全量
   < 2s 可承受偶尔全量重算；如需强一致可在 settings 入口显式 force refresh
   （本期未提供 force API）。

不做什么（D8 红线）：
- 不写磁盘（除本模块自身的 cache 文件）
- 不写 binding
- 不进漂移矩阵列头（候选 ≠ 已纳管）
- 不自动 import SKILL.md
- 触发权完全在用户（Settings 页"加入"按钮）
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from skillos.core.algorithm.hash import FileEntry, list_content_files

_WHITELIST_PATH = Path(__file__).parent / "builtin_potential_runtimes.yaml"

# cache schema 版本：破坏性变更时 +1，旧 cache 自动失效
_CACHE_VERSION = 1
_CACHE_FILENAME = "discover_cache.json"
_TMP_PREFIX = ".discover_cache."

# TTL 兜底：签名不感知「已有 path 内部文件增删」（如在 ~/.claude/skills/ 下
# 新增/删除 skill），超过此阈值强制全量重算。NFR 性能 62 个 skill 全量 < 2s，
# 5 分钟可接受偶尔的过期。daemon 常驻场景下走 cache 命中不重算，重启不破
# 坏 cache（与 daemon 重启幂等约定一致）。
_DISCOVERY_CACHE_TTL_S = 300  # 5 分钟


@dataclass(frozen=True)
class Candidate:
    """一个白名单命中（或未命中）的潜在 Runtime。"""

    key: str
    display_name: str
    root_path: Path
    path_exists: bool
    skill_count: int  # 含 SKILL.md 的子目录数（命中 = >= 1）
    verified_at: date | None
    evidence: str
    skipped: bool  # yaml skip=true → 不探测，仅占位

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.display_name,
            "path": str(self.root_path),
            "path_exists": self.path_exists,
            "skill_count": self.skill_count,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "evidence": self.evidence,
            "skipped": self.skipped,
        }


def _load_whitelist_from_bytes(yaml_bytes: bytes) -> list[dict[str, Any]]:
    """从 yaml bytes 解析白名单。YAMLError 透传（与原版一致）。"""
    data = yaml.safe_load(yaml_bytes)
    return list(data) if data else []


def _parse_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError:
            return None
    return None


def _count_skill_dirs(root: Path) -> int:
    """命中判据（T16 单一入口）：root 下含 SKILL.md 的直接子目录数。

    沿用 list_content_files 单一入口，不另立枚举；list_content_files 在
    root 不存在时抛 FileNotFoundError，由调用方 try/except 转为 0 命中。
    """
    try:
        files: list[FileEntry] = list_content_files(root)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return 0
    # 含 SKILL.md 的直接子目录集合（命中 = 至少 1）
    parents: set[Path] = set()
    for f in files:
        if str(f.path).endswith("SKILL.md"):
            parents.add((root / str(f.path)).parent)
    return len(parents)


# ---------------------------------------------------------------- 签名门控（cache）

def _cache_path(home: Path) -> Path:
    """cache 文件路径，与 daemon 锁同目录（约定统一）。"""
    return home / ".skillos" / _CACHE_FILENAME


def _compute_signature(
    yaml_bytes: bytes,
    key_path_exists: list[tuple[str, str, bool]],
) -> str:
    """signature = sha256(yaml_bytes ‖ sorted([(key, path, exists)]))。

    包含 yaml 自身（改 yaml → 失效）+ 每条 (key, root_path, path_exists)
    （路径增删或存在性变化 → 失效）。**不**包含 skill_count，避免签名计算
    本身要走 list_content_files —— 那样门控就退化成全量重算。
    """
    h = hashlib.sha256()
    h.update(yaml_bytes)
    h.update(b"\x00\xff")  # yaml 段与 path 段分隔（防 yaml 末尾拼到首行 key）
    for key, path, exists in sorted(key_path_exists):
        h.update(f"{key}\x00{path}\x00{1 if exists else 0}\n".encode())
    return h.hexdigest()


def _read_cache(cache_path: Path) -> dict[str, Any] | None:
    """读 cache。失败 / 版本不匹配 / 缺字段 / 超 TTL → 返回 None（触发全量重算）。"""
    try:
        raw = cache_path.read_bytes()
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != _CACHE_VERSION:
        return None
    if not isinstance(data.get("signature"), str):
        return None
    if not isinstance(data.get("results"), list):
        return None
    # TTL 兜底：签名不感知「已有 path 内部文件增删」，超过阈值强制全量
    computed_at = data.get("computed_at")
    if not isinstance(computed_at, str):
        return None
    try:
        ts = datetime.fromisoformat(computed_at)
    except ValueError:
        return None
    if (datetime.now(UTC) - ts).total_seconds() > _DISCOVERY_CACHE_TTL_S:
        return None
    return data


def _write_cache(cache_path: Path, signature: str, items: list[Candidate]) -> None:
    """原子写 cache（best-effort，失败不抛错）。"""
    cache_dir = cache_path.parent
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    data = {
        "version": _CACHE_VERSION,
        "signature": signature,
        "computed_at": datetime.now(UTC).isoformat(),
        "results": [c.to_dict() for c in items],
    }
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(cache_dir), prefix=_TMP_PREFIX, suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_path, cache_path)  # POSIX & Windows 都 atomic
        tmp_path = None
    except OSError:
        pass  # best-effort：门控可接受 cache 失败
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _candidate_from_dict(d: Any) -> Candidate:
    """从 dict 还原 Candidate。缺字段抛 KeyError（外层 try 兜底）。"""
    return Candidate(
        key=str(d["key"]),
        display_name=str(d["name"]),
        root_path=Path(str(d["path"])),
        path_exists=bool(d["path_exists"]),
        skill_count=int(d["skill_count"]),
        verified_at=_parse_date(d.get("verified_at")),
        evidence=str(d.get("evidence", "local-observed")),
        skipped=bool(d.get("skipped", False)),
    )


# ---------------------------------------------------------------- 主入口

def discover_potential_runtimes(home: Path) -> list[Candidate]:
    """按白名单 + home 探测候选（FR-1.0.32 / T24），带签名门控（见模块 docstring）。

    返回顺序按 yaml 出现次序；每条都返回（含未命中：path_exists=False /
    skill_count=0），让前端"未纳管候选"段一次性展示。

    两阶段：
    1. **stat 阶段**（必跑）：读 yaml bytes + 每条 path_exists 算 signature
    2. **count 阶段**（仅 cache 不命中时跑）：每个 path_exists=True 的路径
       调 list_content_files 算 skill_count，写 cache
    """
    # 1. 读 yaml bytes（OSError 透传，与原版一致）
    yaml_bytes = _WHITELIST_PATH.read_bytes()

    # 2. 解析白名单（YAMLError 透传）
    entries = _load_whitelist_from_bytes(yaml_bytes)

    # 3. stat 阶段：白名单解析 + 轻量 stat（不算 count）
    sig_inputs: list[tuple[str, str, bool]] = []
    parsed: list[dict[str, Any]] = []
    for entry in entries:
        key = str(entry.get("key") or "").strip()
        if not key:
            continue
        skipped = bool(entry.get("skip"))
        relpath = str(entry.get("home_relpath") or "")
        try:
            root = Path(relpath.replace("{home}", str(home)))
        except (KeyError, ValueError):
            continue
        if skipped:
            sig_inputs.append((key, str(root), False))
            exists = False
        else:
            exists = root.exists()
            sig_inputs.append((key, str(root), exists))
        parsed.append({
            "key": key,
            "display_name": str(entry.get("display_name") or key),
            "root": root,
            "exists": exists,
            "skipped": skipped,
            "verified_at": _parse_date(entry.get("verified_at")),
            "evidence": str(entry.get("evidence") or "local-observed"),
        })

    # 4. 算签名
    sig = _compute_signature(yaml_bytes, sig_inputs)

    # 5. cache 命中：零 list_content_files 调用，直接从 dict 还原
    cached = _read_cache(_cache_path(home))
    if cached and cached.get("signature") == sig:
        try:
            return [_candidate_from_dict(d) for d in cached["results"]]
        except (KeyError, ValueError, TypeError, OSError):
            pass  # cache 内容损坏 → 降级全量

    # 6. count 阶段：每个 path_exists=True 的算 skill_count
    items: list[Candidate] = []
    for p in parsed:
        if p["skipped"]:
            items.append(Candidate(
                key=p["key"], display_name=p["display_name"],
                root_path=p["root"], path_exists=False, skill_count=0,
                verified_at=p["verified_at"], evidence=p["evidence"],
                skipped=True,
            ))
        else:
            count = _count_skill_dirs(p["root"]) if p["exists"] else 0
            items.append(Candidate(
                key=p["key"], display_name=p["display_name"],
                root_path=p["root"], path_exists=p["exists"], skill_count=count,
                verified_at=p["verified_at"], evidence=p["evidence"],
                skipped=False,
            ))

    # 7. 写 cache（best-effort，失败不抛错）
    _write_cache(_cache_path(home), sig, items)

    return items
