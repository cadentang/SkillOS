"""safe_path —— 路径安全统一入口（04 §2.2.0，FR-1.0.14 / NFR-7.4）。

所有接受外部传入 path 的入口（REST ?path=、WS file_read/file_write、
MCP 工具参数、CLI 参数）必须先过这一个函数，不得各自拼接路径后直接开文件。

约束（04 §2.2.0 表）：
1. 先 resolve() 再比较 —— 否则经符号链接的逃逸检测不出来。
2. 禁止字符串前缀匹配判越界（/data/skills-evil 会误通过 /data/skills 检查）。
3. Windows 上 resolve() 展开 8.3 短名与 UNC 前缀，比较在同一形态下进行。
4. Windows 保留名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）与非法字符一并拒绝。
5. 错误码固定 PATH_TRAVERSAL；错误信息不回显解析后的绝对路径。
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_ERROR_CODE = "PATH_TRAVERSAL"

# Windows 保留设备名（不含扩展名部分比较）
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# Windows FAT/NTFS 非法字符 + 控制字符
_ILLEGAL_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


class PathTraversalError(Exception):
    """路径越界 / 非法。HTTP 400，code=PATH_TRAVERSAL，不回显解析后的绝对路径。"""

    code = _ERROR_CODE

    def __init__(self, rel: str, reason: str = "invalid path"):
        # 注意：只回显用户传入的 rel，不回显解析后的绝对路径（避免泄露目录结构）
        super().__init__(f"{_ERROR_CODE}: {reason}: {rel!r}")
        self.rel = rel
        self.reason = reason


def _validate_component(rel: str, part: str) -> None:
    if part in ("", ".", ".."):
        raise PathTraversalError(rel, "empty or dot component")
    if _ILLEGAL_CHARS.search(part):
        raise PathTraversalError(rel, "illegal character")
    stem = part.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        # CON.txt 也是保留名（stem 比较），macOS 上 CON 合法但按统一口径拒绝
        raise PathTraversalError(rel, "reserved device name")
    if part.endswith((" ", ".")):  # Windows 目录项不允许结尾空格/点
        raise PathTraversalError(rel, "trailing space or dot")


def safe_path(root: Path, rel: str) -> Path:
    """把外部传入的相对路径解析为受限于 root 的绝对路径。

    绝对路径、含 .. 逃逸、经 symlink 逃逸、命中保留名 —— 一律抛 PathTraversalError。
    """
    if not isinstance(rel, str) or not rel.strip():
        raise PathTraversalError(str(rel), "empty path")
    if Path(rel).is_absolute() or rel.startswith(("/", "\\")):
        raise PathTraversalError(rel, "absolute path not allowed")
    if "\x00" in rel:
        raise PathTraversalError(rel, "null byte")

    for part in re.split(r"[\\/]+", rel):
        _validate_component(rel, part)

    base = root.resolve()
    target = (base / rel).resolve()  # 展开 symlink 与 Windows 8.3 短名
    if not target.is_relative_to(base):
        raise PathTraversalError(rel, "escapes root")
    return target


def path_key(rel: str) -> str:
    """路径键归一化（FR-1.0.31 / 04 §2.2.0-10 / 06 A7）。

    按分隔符分段，逐段 NFC + casefold，用于"两个路径是否同一个"的比较。
    坑：macOS 写盘即 NFD 化、Windows 大小写不敏感 —— 只比大小写会漏掉
    中文 / 组合字符场景（本机 62 个 Skill 全中文名）。
    注意：key 只用于比较，不得替换用户可见的真实路径。
    """
    parts = [unicodedata.normalize("NFC", p).casefold() for p in re.split(r"[\\/]+", rel) if p]
    return "/".join(parts)
