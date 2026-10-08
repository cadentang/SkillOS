"""三入口共享的全局写锁（FR-1.0.17 / 05 P0-3）。

为什么锁在 services 层而不是 daemon 内：CLI、daemon、MCP 都能写同一批
Runtime 目录，只在 daemon 内加锁等于「daemon push 时终端敲 skillos push 会互踩」。
锁下沉到写操作入口（services → core/fs 写函数第一道关），三模式自动继承。

实现约束：
- 用 filelock（纯 Python、跨平台）；禁用 fcntl / msvcrt（Windows 上 import 即崩）。
- 锁文件 ~/.skillos/.lock（沙盒下由调用方改传容器内路径）。
- 超时 30s，超时报错退出，不静默跳过。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from skillos.core.constants import LOCK_TIMEOUT_S


class LockTimeout(Exception):
    """获取写锁超时 —— 显式失败，不静默跳过（04 §2.5.2）。"""


@contextmanager
def write_lock(lock_dir: Path, timeout: int = LOCK_TIMEOUT_S) -> Iterator[FileLock]:
    """全局写锁。lock_dir 由调用方传入（沙盒下指向容器内目录，T1）。

    持有时长覆盖整个写事务。
    """
    lock = FileLock(str(lock_dir / ".lock"), timeout=timeout)
    try:
        lock.acquire()
    except Timeout as e:
        raise LockTimeout(
            f"另一个 skillos 写操作正在运行（等待 {timeout}s 超时）"
        ) from e
    try:
        yield lock
    finally:
        lock.release()
