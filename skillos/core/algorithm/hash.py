"""哈希两级结构（04 §2.1.3 哈希口径，FR-1.0.5 / T16）。

第一级 hash_bytes：字节级、绝不归一化 —— 是"存储身份"，写入 installed_hash 的就是它。
第二级 compare_normalized：仅当字节 hash 已判定不等时调用，回答"差异是否仅风格差异"；
  只折叠文本、孤立 \\r 不动、二进制不折叠（06 B1）。
  它的结果只用于 UI 提示，不得回流影响任何身份判定。

枚举单一化：list_content_files() 是唯一的文件枚举入口，scan / hash / diff /
removals 守卫共用；排除集三类定义于此，别处不得另立一份。
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import Path

from skillos.core.constants import ASSET_MAX_BYTES
from skillos.core.model import FileEntry

# ---------------------------------------------------------------- 排除集（三类，FR-1.0.5）

# ① 影响 hash：这些路径不进枚举、不进哈希
# node_modules / .runtime / .venv 由 04 §2.2.1 规则 3 与排期 P1 一并纳入统一排除集
HASH_EXCLUDE_DIR_NAMES: frozenset[str] = frozenset(
    {"__pycache__", ".git", ".skillos", "node_modules", ".runtime", ".venv"}
)
HASH_EXCLUDE_FILE_NAMES: frozenset[str] = frozenset({".DS_Store"})
HASH_EXCLUDE_FILE_SUFFIXES: tuple[str, ...] = (".pyc",)

# ③ 纯噪音：日志 / 临时文件 / 交换残留（FR-1.0.15 残留形态 .old / .<name>.tmp-* / .staged-*）
NOISE_FILE_SUFFIXES: tuple[str, ...] = (".log", ".old")
NOISE_NAME_MARKERS: tuple[str, ...] = (".tmp-", ".staged-")


def is_excluded(rel_posix: str) -> bool:
    """相对路径（POSIX 分隔符）是否被排除出内容枚举。① 与 ③ 两类在此合并判定。"""
    parts = rel_posix.split("/")
    for part in parts:
        if part in HASH_EXCLUDE_DIR_NAMES or part in HASH_EXCLUDE_FILE_NAMES:
            return True
        if part.endswith(HASH_EXCLUDE_FILE_SUFFIXES):
            return True
        if part.endswith(NOISE_FILE_SUFFIXES):
            return True
        # 残留形态 .<name>.tmp-<rand>/：marker 在名字中间而非开头，用包含判定
        if any(marker in part for marker in NOISE_NAME_MARKERS):
            return True
    return False


# ---------------------------------------------------------------- 第一级：字节级哈希


def hash_bytes(content: bytes) -> str:
    """单文件内容哈希。字节级，不做任何归一化（T16）。"""
    return hashlib.sha256(content).hexdigest()


def aggregate_hash(files: Mapping[str, bytes]) -> str:
    """目录级哈希：sha256(posix_path + NUL + sha256(content)) 排序后聚合。

    - 路径统一 POSIX 分隔符（05 P2-3）
    - 内容字节级、不归一化（T16：归一化会让"两端来回互推"成为常态）
    - 排序保证与遍历顺序无关（纯函数前提，NFR-3.5）
    """
    nul = "\x00"
    digests = [
        p.replace(chr(92), "/") + nul + hash_bytes(files[p])
        for p in sorted(files)
    ]
    return hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()


def aggregate_entries(entries: Iterable[FileEntry]) -> str:
    """按 FileEntry 聚合：in_git=False 的超阈值资产不参与（FR-1.0.20）。"""
    return aggregate_hash({e.path: e.content for e in entries if e.in_git})


# ---------------------------------------------------------------- 第二级：兜底比较


def is_foldable_text(content: bytes) -> bool:
    """可折叠文本判定：UTF-8 可解码且不含 NUL（二进制一律不折叠，06 B1）。"""
    if b"\x00" in content[:8192]:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _fold(content: bytes) -> str:
    """折叠规则：CRLF→LF（孤立 \\r 不动）、去 BOM、NFC、行尾空白。仅用于解释差异。"""
    if content.startswith(b"\xef\xbb\xbf"):
        content = content[3:]
    # 只替换 \r\n，孤立 \r 保留（可能是脚本里有意义的字节）
    text = content.replace(b"\r\n", b"\n").decode("utf-8")
    text = unicodedata.normalize("NFC", text)
    lines = [line.rstrip(" \t") for line in text.split("\n")]
    return "\n".join(lines)


def compare_normalized(a: bytes, b: bytes) -> bool:
    """第二级兜底比较：报告"差异是否仅风格差异"。

    前置：仅在 hash_bytes 已判定 a != b 后调用（相同则无须比较）。
    结果只供 UI 提示；不得参与漂移状态判定（T16）。
    """
    if a == b:
        return True
    if not (is_foldable_text(a) and is_foldable_text(b)):
        return False  # 二进制不折叠
    return _fold(a) == _fold(b)


# ---------------------------------------------------------------- 唯一枚举入口


def list_content_files(root: Path) -> list[FileEntry]:
    """枚举 root 下的内容文件（相对 root 的 POSIX 路径）。

    - 排除集由 is_excluded() 统一判定（① + ③），不在此另立
    - 超过 ASSET_MAX_BYTES 的单文件标记 in_git=False、内容不读入（T2）
    - is_text 由 is_foldable_text 判定（供 diff / 渲染层参考）
    - 顺序确定（按路径排序），与遍历顺序无关
    - **遍历遇错显式抛出**（NFR-11.3）：静默跳过会让 removals 守卫把
      「读不了的目录」当「空目录」，漏报会消失的文件
    """
    root = root.resolve()
    if not root.exists():
        raise FileNotFoundError(f"list_content_files: {root} 不存在（无法判断其内容，不许猜）")
    if not root.is_dir():
        raise NotADirectoryError(f"list_content_files: {root} 不是目录")

    def _raise_walk_error(err: OSError) -> None:
        raise err

    out: list[FileEntry] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=_raise_walk_error):
        cur = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not is_excluded((cur / d).relative_to(root).as_posix())
        )
        for name in sorted(filenames):
            path = cur / name
            rel = path.relative_to(root).as_posix()
            if is_excluded(rel):
                continue
            size = path.stat().st_size
            if size > ASSET_MAX_BYTES:
                # T2：超阈值资产不纳管、不参与漂移哈希；内容按需由分发层单独读
                out.append(FileEntry(path=rel, content=b"", in_git=False, is_text=False))
                continue
            content = path.read_bytes()
            out.append(
                FileEntry(path=rel, content=content, in_git=True, is_text=is_foldable_text(content))
            )
    return out
