"""WebSocket 连接管理（FR-3.8）。

两个通道：
- /ws/agent：daemon 出站连接（hello / task / result / event / ping-pong）
- /ws/ui：前端订阅（task_progress / task_done / drift_changed / daemon_status）

断连语义（04 §4.2）：断连不等于失败 —— 任务未确认完成落 unknown，重连后补发
未 ack 的 result（按 task_id 去重），前端进度条冻结不伪造成败。
"""

from __future__ import annotations


class WsHub:
    """连接注册表 + 通道隔离。实现待 P3。"""

    pass
