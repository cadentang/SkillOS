"""mcp —— MCP Server 入口（FR-1.3.x，stdio 传输）。

工具清单（4.3，白名单）：
  skillos_list_skills / skillos_get_skill / skillos_status / skillos_check /
  skillos_version_log / skillos_sync_push / skillos_sync_pull / skillos_upgrade

约束：
1. 写入类工具默认 dry_run=true（FR-1.3.3），需显传 dry_run=false 才落盘。
2. 不暴露 force（FR-1.3.5）：无 on_conflict 参数，冲突只能 abort / skip。
3. 不暴露通用 git / shell（OUT-14）：只有领域操作。
4. 无设备管理工具：token 撤销只在 Web 人工路径。
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError("P2 实现：官方 MCP SDK stdio server")


if __name__ == "__main__":
    main()
