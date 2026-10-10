"""结构化日志系统（T23，FR-1.1.11，04 §6.2）。

为什么自建而不是直接 `logging.basicConfig`：
1. structlog 的 `bind_contextvars` 跨调用栈自动传递 `op_id` 等字段
   —— 比手工 `extra={"op_id": ...}` 安全（不会出现"忘了塞某个字段"）。
3. Scrubber 字段对敏感字段名（token/secret/cookie 等）整体置红
   —— 是「禁止记录凭据」(NFR-2 / 04 §7) 的代码层落地。
4. 与 stdlib 完全兼容：FastAPI / uvicorn / 任意 stdlib 第三方 logger
   通过 `ProcessorFormatter` 自动接入；未来接 OTel / Loki 不绕路。

分层（与 04 §6.2.4 Sink 表一致）：
- daemon / api（云端）：JSON 到 stderr + 文件 ~/.skillos/logs/<mode>.log 10MB×5
- mcp（stdio）：JSON 到 stderr；**不落文件**（JSON-RPC 协议敏感）

CLI 模式：`get_logger` 拿到的 logger 默认沉默（DEBUG/INFO 不出 stderr）；
由 `SKILLOS_LOG_LEVEL=INFO` 激活 —— CLI 用户输出仍走 `console.print`，
不归 logger（04 §6.2.4 表第 1 行）。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import uuid
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Literal

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, merge_contextvars
from structlog.stdlib import BoundLogger, LoggerFactory, ProcessorFormatter

# 模式：cli / daemon / api / mcp。决定 sink 拓扑（04 §6.2.4）。
LogMode = Literal["daemon", "api", "mcp"]

# 敏感字段名集合（04 §6.2.3 末段；红绿红字段二保险）
_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "token", "password", "passwd", "secret",
    "authorization", "cookie", "set-cookie",
    "jwt", "pair_code", "api_key", "apikey",
    "private_key", "client_secret",
})

# 红化常量
_REDACTED: str = "***REDACTED***"

# 4xx 错误消息中的路径 / 内容字段名 —— 命中即 drop（保留 path / hash / bytes）
_DROP_KEYS: frozenset[str] = frozenset({
    "content", "file_content", "body", "raw",
})


# ---------------------------------------------------------------- op_id 动态生成

def new_op_id() -> str:
    """生成 op_id。

    优先 uuid7（时间序，便于按时间排序）；当前 Python 3.13+ 已支持（PEP 745）。
    """
    try:
        return f"op_{uuid.uuid7().hex}"
    except AttributeError:
        # 兼容老版本回退 uuid4 —— 不影响功能，仅失去时间序优势
        return f"op_{uuid.uuid4().hex}"


def bind_op_id(op_id: str | None = None, **extra: Any) -> str:
    """绑定 op_id 与其它业务字段到 contextvars。返回 op_id。

    structlog 的 `bind_contextvars` 是异步安全且线程安全的；
    同线程后续所有 log.info / log.warning 自动带上这些字段，
    不需要手工传递 —— 这是 T23 「op_id 跨调用栈自动传递」的代码层落地。
    """
    op = op_id or new_op_id()
    bind_contextvars(op_id=op, **extra)
    return op


def reset_op_context() -> None:
    """清空 contextvars。HTTP / WS 请求结束 / CLI 命令结束时调用。

    不清会导致 op_id 跨请求泄漏（异步任务偶发的"上一请求的连接信息"污染）。
    """
    clear_contextvars()


def get_op_id() -> str | None:
    """读取当前 op_id（用于给响应 header / CLI 输出回填）。"""
    ctx = structlog.contextvars.get_contextvars()
    return ctx.get("op_id") if ctx else None


# ---------------------------------------------------------------- 主初始化

_initialized: bool = False
_init_lock = threading.Lock()


def _configure_stdlib_bridge() -> None:
    """structlog → stdlib 桥接的最小配置（import 即生效）。

    为什么 import 时就配：structlog 未 configure 时默认走 PrintLogger
    （直接 print 到 stdout），会绕过 stdlib handler —— 测试进程 / 任何
    没调 setup_logging 的进程（caplog、uvicorn 内置 logger 桥接、文件
    sink）全部失效。import 时先把它拉回 stdlib 路径，setup_logging
    之后只负责挂 handler / 设级别。
    """
    structlog.configure(
        processors=[
            merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            _add_pid_tid,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            structlog.processors.format_exc_info,
            _scrub_sensitive,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def setup_logging(
    mode: LogMode,
    *,
    level: str | int | None = None,
    home: Path | None = None,
) -> None:
    """初始化日志系统。每个进程入口调一次。

    Args:
        mode: cli / daemon / api / mcp
        level: 覆盖默认级别；缺省按 SKILLOS_LOG_LEVEL 环境变量 / 默认 INFO
        home: 本机 home（决定 ~/.skillos/logs/）；CLI 默认 None 不创建日志目录
    """
    global _initialized
    with _init_lock:
        if _initialized:
            return
        _initialized = True

        # 1) 决定全局级别（CLI 默认 WARNING，让 user-facing 输出走 console.print）
        default_level = "WARNING" if mode == "cli" else "INFO"
        if level is None:
            level = os.environ.get("SKILLOS_LOG_LEVEL", default_level)
        if isinstance(level, str):
            level = level.upper()

        # 2) 决定 sink 拓扑
        #    - daemon / api：stderr（JSON） + 文件旋转
        #    - mcp：stderr（JSON），不落文件（stdio 协议敏感）
        #    - cli：仅 stderr（默认 WARNING），不落文件
        handlers: list[logging.Handler] = []

        stderr_handler = logging.StreamHandler(stream=sys.stderr)
        stderr_handler.setFormatter(_build_formatter(human=(mode == "cli")))
        handlers.append(stderr_handler)

        if mode != "cli" and mode != "mcp" and home is not None:
            log_path = _log_path(home, mode)
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                file_handler = logging.handlers.RotatingFileHandler(
                    filename=str(log_path),
                    maxBytes=10 * 1024 * 1024,  # 10MB（04 §6.2.4）
                    backupCount=5,
                    encoding="utf-8",
                )
                file_handler.setFormatter(_build_formatter(human=False))
                handlers.append(file_handler)
            except OSError:
                # 文件创建失败（沙盒内只读 FS / 权限不足）降级到 stderr only
                pass

        # 3) 配置 stdlib root logger
        root = logging.getLogger()
        root.setLevel(level)
        # 清理已有 handler（防止 reload / 测试时重复挂载）
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in handlers:
            root.addHandler(h)

        # 4) structlog → stdlib 桥接已在 import 时配置（_configure_stdlib_bridge）；
        #    此处无需重复 configure —— handler / 级别变化只影响 stdlib root logger。


def get_logger(name: str | None = None) -> BoundLogger:
    """取 logger。已绑定 pid/tid/op_id 等字段。

    用法：
        log = get_logger("skillos.daemon")
        bind_op_id("op_xxx")  # 一次绑定，全栈生效
        log.info("daemon_started")  # 自动含 op_id
    """
    return structlog.get_logger(name)  # type: ignore[return-value]


# ---------------------------------------------------------------- 辅助

def _log_path(home: Path, mode: LogMode) -> Path:
    return home / ".skillos" / "logs" / f"{mode}.log"


def _build_formatter(*, human: bool) -> logging.Formatter:
    """构造 stdlib Formatter —— 走 ProcessorFormatter 桥接 structlog event。
    human=True 时输出 key=value（CLI 调试用）；False 时输出 JSON。
    """
    if human:
        # CLI 模式：人眼友好（key=value，单行）
        return ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=False),
            foreign_pre_chain=[
                merge_contextvars,
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                _add_pid_tid,
                structlog.processors.TimeStamper(fmt="iso", utc=False),
                _scrub_sensitive,
            ],
        )
    return ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=[
            merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            _add_pid_tid,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            _scrub_sensitive,
        ],
    )


def _add_pid_tid(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """注入 pid / tid（每行**）。"""
    event_dict.setdefault("pid", os.getpid())
    event_dict.setdefault("tid", threading.get_ident())
    return event_dict


def _scrub_sensitive(
    _: Any, __: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Scrubber 字段（04 §6.2.3）：

    1. 字段名（大小写不敏感）匹配 _SENSITIVE_KEYS → 置 _REDACTED
    2. 字段名匹配 _DROP_KEYS（文件内容类）→ 直接删字段
    3. 字段值是 dict/list 时递归清洗
    """
    _scrub_in_place(event_dict)
    return event_dict


def _scrub_in_place(d: MutableMapping[str, Any]) -> None:
    """就地清洗。"""
    keys_to_drop: list[str] = []
    for k, v in list(d.items()):
        if not isinstance(k, str):
            continue
        kl = k.lower()
        if kl in _DROP_KEYS:
            keys_to_drop.append(k)
            continue
        if kl in _SENSITIVE_KEYS:
            d[k] = _REDACTED
            continue
        if isinstance(v, MutableMapping):
            _scrub_in_place(v)
        elif isinstance(v, (list, tuple)):
            cleaned = [_scrub_value(item) for item in v]
            d[k] = type(v)(cleaned)
    for k in keys_to_drop:
        d.pop(k, None)


def _scrub_value(v: Any) -> Any:
    if isinstance(v, MutableMapping):
        _scrub_in_place(v)
        return v
    if isinstance(v, str) and _looks_like_token(v):
        # 字符串值形态像 token / JWT：保守红
        return _REDACTED
    return v


def _looks_like_token(s: str) -> bool:
    """粗略识别「可能是凭据」的字符串值（防御性，避免误带 token 进日志）。

    命中条件：长度 ≥ 32 且含 base64-like 字符 → 视作可疑 → 红化。
    """
    if len(s) < 32:
        return False
    # 含 jwt 经典段 "."
    if s.count(".") == 2 and len(s.split(".")) == 3:
        return True
    # 看起来像 token 的长串
    if any(c in s for c in ("=", "/", "+")) and s.replace("-", "").replace("_", "").isalnum():
        return True
    return False


# import 即生效：任何 import 本模块的进程都走 stdlib 桥接（见函数 docstring）。
# 必须放在文件末尾（依赖上面已定义的 _add_pid_tid / _scrub_sensitive）。
_configure_stdlib_bridge()