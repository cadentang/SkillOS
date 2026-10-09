"""三入口共享的全局写锁（FR-1.0.17 / 05 P0-3）。

为什么锁在 services 层而不是 daemon 内：CLI、daemon、MCP 都能写同一批
Runtime 目录，只在 daemon 内加锁等于「daemon push 时终端敲 skillos push 会互踩」。
锁下沉到写操作入口（services → core/fs 写函数第一道关），三模式自动继承。

实现约束：
- 用 filelock（纯 Python、跨平台）；禁用 fcntl / msvcrt（Windows 上 import 即崩）。
- 锁文件 ~/.skillos/.lock（沙盒下由调用方改传容器内路径）。
- 超时 30s，超时报错退出，不静默跳过。

锁日志（T23 / 04 §6.2.6）：
- lock_wait_started / lock_wait_succeeded / lock_wait_timeout
- lock_acquired / lock_released
- lock_busy（含持锁者 pid / host / operation / start_time）
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from skillos.core.constants import LOCK_TIMEOUT_S
from skillos.core.logging import get_logger

log = get_logger("skillos.services.lock")


class LockTimeout(Exception):
    """获取写锁超时 —— 显式失败，不静默跳过（04 §2.5.2）。"""


@contextmanager
def write_lock(lock_dir: Path, timeout: int = LOCK_TIMEOUT_S,
               operation: str = "unknown") -> Iterator[FileLock]:
    """全局写锁。lock_dir 由调用方传入（沙盒下指向容器内目录，T1）。

    持有时长覆盖整个写事务。

    Args:
        lock_dir: 锁文件所在目录（`~/.skillos/` 或沙盒内对应目录）
        timeout: 等待秒数（前台 20s，后台 30s）
        operation: 操作名（如 "push_skill" / "rollback_skill"），仅用于日志
    """
    lock_path = lock_dir / ".lock"
    lock = FileLock(str(lock_path), timeout=timeout)
    t0 = time.monotonic()
    log.debug(
        "lock_wait_started",
        operation=operation, timeout_s=timeout,
    )
    try:
        lock.acquire()
    except Timeout as e:
        # 持锁者诊断（FR-2.4.9）：读 filelock 默认写入的持有者文本
        holder_pid = ""
        holder_host = ""
        try:
            holder_text = lock_path.read_text(encoding="utf-8").strip()
            # filelock 默认写 "<pid>\n<host>"（Unix 上）
            holder_pid = holder_text.split("\n", 1)[0] if holder_text else ""
            holder_host = (
                holder_text.split("\n", 1)[1]
                if "\n" in holder_text else ""
            )
        except OSError:
            pass
        log.warning(
            "lock_wait_timeout",
            operation=operation, timeout_s=timeout,
            elapsed_s=round(time.monotonic() - t0, 2),
            holder_pid=holder_pid or None,
            holder_host=holder_host or socket.gethostname(),
        )
        raise LockTimeout(
            f"另一个 skillos 写操作正在运行（{operation}，等待 {timeout}s 超时）"
        ) from e
    log.debug(
        "lock_acquired",
        operation=operation,
        elapsed_s=round(time.monotonic() - t0, 3),
    )
    try:
        yield lock
    finally:
        lock.release()
        log.debug(
            "lock_released",
            operation=operation,
            held_s=round(time.monotonic() - t0, 3),
        )
