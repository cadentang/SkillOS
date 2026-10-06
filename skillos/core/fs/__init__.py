"""core/fs —— 本地专用文件系统层（涉及 IO）。

T1 落地约束（04 §3 红线 2/3）：
- 全部文件操作显式接收 root: Path，禁止从全局配置 / 单例 / 环境变量隐式读取。
- 路径校验统一走 safe_path()，不散落各处；不引入 VFS / Storage Provider 抽象层。

沙盒隔离即靠把 root 指向容器内虚拟目录实现（SKILLOS_ROOT），本层无感知。
"""
