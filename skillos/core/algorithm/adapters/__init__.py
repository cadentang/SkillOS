"""运行时适配器（04 §2.1.2，FR-1.0.8）。

canonical → 目标 Runtime 的结构与字段转换。

纯函数红线（T6 / T7）：
- render_layout() 同一 SkillSnapshot + 同一 adapter.version 调用两次，输出逐字节相同。
  禁止依赖时间戳、随机数、环境变量、本机路径。
- 改字段策略 / sidecar 模板 / 目录布局 ⇒ 必须 bump version。
"""
