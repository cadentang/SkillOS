"""Skill OS 编译发行的**统一**入口（multicall dispatcher）。

为什么是一个二进制而不是三个
----------------------------
skillos 的三个角色依赖集高度重叠（pydantic / sqlalchemy / fastapi / uvicorn /
mcp / anyio / typer）。各做一份 standalone，会得到三份几乎相同的运行时
（每份约 120–180MB），发行物体积直接 ×3。

因此默认只对**本文件**做一次 standalone 构建，再把同一个可执行文件在
发行目录内复制成三个名字，按 `argv[0]` 的基名分派：

    skillos          → CLI（含 `skillos daemon` 子命令，daemon 即走这里）
    skillos-mcp      → MCP stdio 服务
    skillos-server   → 后端 ASGI 服务
    skillos-daemon   → 等价 `skillos daemon`

复制体与主程序**同目录**，Nuitka standalone 的相对查找（python3xx.dll、
模块目录）不受影响。

两道兜底
--------
`argv[0]` 在被改名、被某些 shell / 打包器改写时可能拿不到原名，所以额外
支持显式子命令 `skillos serve`（起后端）。MCP 与 daemon **不需要**兜底 ——
`skillos mcp` / `skillos daemon` 本来就是产品 CLI 的子命令，直接可用。
"""

from __future__ import annotations

import os
import sys

# 必须在任何 skillos import 之前钉死编码与 UTF-8 模式：
#   * Windows 默认 cp936 控制台输出中文必抛 UnicodeEncodeError（05 P0-2）
#   * MCP stdio 走二进制管道，PYTHONUTF8 保证 JSON-RPC 不被转码
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _force_stdio() -> None:
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _basename(argv0: str) -> str:
    base = os.path.basename(argv0 or "").lower()
    for suffix in (".exe", ".bin", ".cmd", ".sh"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.replace("_", "-")


def _resolve_role(argv0: str, argv: list[str]) -> tuple[str, list[str]]:
    """返回 (role, 剩余参数)。role ∈ {"cli","mcp","server","daemon"}。

    原则：**只拦截产品 CLI 没有的角色**。
      * `skillos mcp` / `skillos daemon` 本来就是产品的子命令 → 原样交给 CLI，
        不在这里重复实现（否则两处实现必然分叉）。
      * `serve` 产品没有 → 由本 dispatcher 承接为「起后端 ASGI」。
    """
    base = _basename(argv0)
    if base.endswith("mcp"):
        return "mcp", argv
    if base.endswith("server") or base.endswith("serve"):
        return "server", argv
    if base.endswith("daemon"):
        return "daemon", argv

    # argv[0] 给不出身份 → 看显式首参里有没有产品没有的那个
    if argv and argv[0] == "serve":
        return "server", argv[1:]

    return "cli", argv


def _run_cli(argv: list[str]) -> None:
    from skillos.cli.main import app  # noqa: PLC0415

    sys.argv = [sys.argv[0] or "skillos", *argv]
    app()


def _run_mcp() -> None:
    from skillos.mcp.server import main as mcp_main  # noqa: PLC0415

    mcp_main()


def _run_server() -> None:
    # 与 skillos_server.py 共用同一份实现，避免两处漂移
    from skillos_server import run_server  # noqa: PLC0415

    run_server()


def main() -> None:
    _force_stdio()
    role, argv = _resolve_role(sys.argv[0] if sys.argv else "", list(sys.argv[1:]))
    if role == "mcp":
        _run_mcp()
    elif role == "server":
        _run_server()
    elif role == "daemon":
        _run_cli(["daemon", *argv])
    else:
        _run_cli(argv)


if __name__ == "__main__":
    main()
