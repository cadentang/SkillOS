"""daemon —— 本地常驻进程（FR-1.2.x，数据面）。

启动序列（04 §2.5.2）：
  读配置 ~/.skillos/config.toml
  → 残留自检（FR-1.0.15）+ 环境校验（WSL 边界 / 云同步目录 / 编码）
  → 加载 token（OS 凭据库优先，回退 0600 文件；禁止入项目目录，NFR-2）
  → WS 连接（指数退避 1s,2s,4s,…,60s 上限）
  → hello（agent_id / version / runtimes）
  → watchdog 监听四个 runtime 目录
  → 任务循环（单任务串行；写操作取全局写锁，与 CLI / MCP 共用）

WSL 边界（NFR-7.7）：daemon 必须与 Runtime 同一文件系统命名空间，
混用（daemon 在 WSL、Agent 在 Windows）明确不支持，启动校验拦截。
"""

from __future__ import annotations
