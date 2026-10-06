"""Skill OS —— 个人 AI Skill 操作系统。

交付物：skillos Python 包（CLI / daemon / MCP 三种运行模式，同一个包）。
分层方向单向：入口(cli/daemon/mcp/api) → services → core/store，core 不得反向依赖。
"""

__version__ = "0.1.0"
