"""core —— 引擎层（algorithm 纯函数无 IO；fs 本地专用涉及 IO）。

硬约束（04 §1.3）：
- core 不得 import services / store / 任何入口层。
- core/algorithm 不得有 IO。
- core/fs 的文件操作必须显式接收 root: Path（T1），禁止隐式读取全局配置 / 环境变量。
"""
