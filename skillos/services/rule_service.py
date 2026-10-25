"""规则集编排（N-03.1/5 / N-08.1/2，`docs/12` §6.1~6.2）。

本层只做四件事：**落位（首启拷贝）→ 加载（带缓存）→ 指纹（ruleset_hash）→ 白名单过滤**。
解析与匹配全在 `core/algorithm/rule_engine.py`（纯函数 + 无代码执行面）。

两条设计取舍：

- **首启拷贝、不覆盖**（N-08.1）：`security.yaml` 拷贝进仓库 `.skillos/rules/` 后归用户所有，
  随 git 版本化、可改级别可删。升级包不覆盖用户改动 —— 否则"用户调过的级别"会被静默改回去。
- **ruleset_hash 只认文件字节**（N-03.5）：同一份规则文件两次 check 必须得同一个 hash，
  与 mtime、加载顺序无关，否则"历史结果可复现"就无从谈起。
"""

from __future__ import annotations

import fnmatch
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from skillos.core.algorithm import rule_engine
from skillos.core.algorithm.rule_engine import (
    CustomRule,
    InvalidRule,
    LoadResult,
    load_rules,
)

RULES_DIR_REL = ".skillos/rules"
IGNORE_FILE = "ignore.yaml"
BUILTIN_SECURITY_FILE = "security.yaml"

_BUILTIN_DIR = Path(rule_engine.__file__).parent / "adapters" / "rules"


@dataclass(frozen=True)
class IgnoreEntry:
    """一条白名单（N-08.2）：`rule_id + path 模式 + 理由`，随 git 版本化。"""

    rule_id: str
    paths: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class RulePanel:
    """规则面板数据（M3-05）：生效 / 无效 / 白名单三块，全部可审计。"""

    rules: tuple[CustomRule, ...]
    invalid: tuple[InvalidRule, ...]
    ignores: tuple[IgnoreEntry, ...]
    ruleset_hash: str
    files: tuple[str, ...]


class RuleService:
    """仓库内规则集：按文件指纹缓存，避免逐 skill 重复解析。"""

    def __init__(self, repo_root: Path, *, rules_dir: Path | None = None,
                 builtin_dir: Path | None = None) -> None:
        self.repo_root = repo_root
        self.rules_dir = rules_dir or (repo_root / RULES_DIR_REL)
        self._builtin_dir = builtin_dir or _BUILTIN_DIR
        self._cache: LoadResult | None = None
        self._fingerprint: tuple[tuple[str, int, int], ...] | None = None
        self._hash: str | None = None
        self._ignores: tuple[IgnoreEntry, ...] | None = None

    # ---------------------------------------------------------------- 落位

    def ensure_builtin(self) -> list[str]:
        """首启把随包安全规则拷贝进仓库（N-08.1）。返回发生变化的文件清单。

        已存在则**不动**（用户可能改过级别/删过规则）；缺失才写。
        """
        actions: list[str] = []
        if not self._builtin_dir.is_dir():
            return actions
        self.rules_dir.mkdir(parents=True, exist_ok=True)
        for src in sorted(self._builtin_dir.glob("*.yaml")):
            dst = self.rules_dir / src.name
            if dst.exists():
                continue
            shutil.copyfile(src, dst)
            actions.append(f"copied {src.name}")
        if actions:
            self._cache = None
            self._ignores = None
        return actions

    # ---------------------------------------------------------------- 加载

    def load(self, *, force: bool = False) -> LoadResult:
        fingerprint = self._file_fingerprint()
        if not force and self._cache is not None and fingerprint == self._fingerprint:
            return self._cache
        result = load_rules(self.rules_dir)
        self._cache = result
        self._fingerprint = fingerprint
        self._hash = None          # 指纹变了，hash 必须重算
        return result

    def _rule_files(self) -> tuple[Path, ...]:
        if not self.rules_dir.is_dir():
            return ()
        return tuple(
            p for p in sorted(self.rules_dir.glob("*.y*ml"))
            if p.is_file() and p.name != IGNORE_FILE
        )

    def _file_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        return tuple(
            (p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in self._rule_files()
        )

    def ruleset_hash(self) -> str:
        """生效规则文件的聚合 hash（N-03.5）：只看字节，不看 mtime。

        文件名也参与 —— 否则"把规则从 a.yaml 挪到 b.yaml"这种改动会得出相同 hash。
        """
        if self._hash is not None:
            return self._hash
        digest = hashlib.sha256()
        for path in self._rule_files():
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        self._hash = digest.hexdigest()[:32]
        return self._hash

    # ---------------------------------------------------------------- 白名单

    def ignores(self) -> tuple[IgnoreEntry, ...]:
        """读 `ignore.yaml`（N-08.2）。格式不合法 / 没有理由的条目**不收**。

        「没有理由的白名单等于后门」—— 需求要求理由可审计，所以这是硬门槛。
        """
        if self._ignores is not None:
            return self._ignores
        path = self.rules_dir / IGNORE_FILE
        if not path.is_file():
            self._ignores = ()
            return self._ignores
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            self._ignores = ()
            return self._ignores
        entries: list[IgnoreEntry] = []
        items = raw.get("ignores") if isinstance(raw, dict) else None
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                rule_id, reason = item.get("rule_id"), item.get("reason")
                if (
                    not isinstance(rule_id, str)
                    or not isinstance(reason, str)
                    or not reason.strip()
                ):
                    continue
                paths = item.get("paths") or ["**"]
                if isinstance(paths, str):
                    paths = [paths]
                if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                    continue
                entries.append(IgnoreEntry(rule_id=rule_id, paths=tuple(paths),
                                           reason=reason.strip()))
        self._ignores = tuple(entries)
        return self._ignores

    def ignored(self, rule_id: str, path: str) -> IgnoreEntry | None:
        """命中白名单返回该条目（调用方可把它作为 info 落库，N-08.2 可审计）。"""
        for entry in self.ignores():
            if entry.rule_id != rule_id:
                continue
            if any(fnmatch.fnmatch(path or "", pat) for pat in entry.paths):
                return entry
        return None

    # ---------------------------------------------------------------- 面板

    def panel(self) -> RulePanel:
        loaded = self.load()
        return RulePanel(
            rules=loaded.rules,
            invalid=loaded.invalid,
            ignores=self.ignores(),
            ruleset_hash=self.ruleset_hash(),
            files=loaded.files,
        )

    def panel_dict(self) -> dict:
        panel = self.panel()
        return {
            "ruleset_hash": panel.ruleset_hash,
            "files": list(panel.files),
            "rules": [rule_engine.as_dict(r) for r in panel.rules],
            "invalid": [
                {"rule_id": i.rule_id, "reason": i.reason, "source": i.source}
                for i in panel.invalid
            ],
            "ignores": [
                {"rule_id": i.rule_id, "paths": list(i.paths), "reason": i.reason}
                for i in panel.ignores
            ],
        }

    def invalidate(self) -> None:
        self._cache = None
        self._fingerprint = None
        self._hash = None
        self._ignores = None
