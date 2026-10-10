"""MCP stdio 单角色入口（`shared_runtime=false` 时按 target 独立构建用）。

关键约束（与源码形态一致，见 skillos/mcp/server.py）：
  1. stdio 模式下**只走 stderr、不落文件**日志（04 §6.2.4 Sink 表）——
     任何写 stdout 的东西都会污染 JSON-RPC 流。
  2. Windows 下必须保证 stdin/stdout 不被文本模式转码；本文件在任何
     inport 之前设置 PYTHONUTF8 / PYTHONIOENCODING，且只对 **stderr**
     做 reconfigure，**不碰 stdout**（stdout 归 SDK 的二进制管道）。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass


def main() -> None:
    from skillos.mcp.server import main as mcp_main

    mcp_main()


if __name__ == "__main__":
    main()
