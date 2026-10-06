# sandbox/ —— 沙盒 Runtime（FR-2.4.3 / FR-3.10）
#
# 沙盒与云端共用同一镜像，仅环境变量切换：
#   SKILLOS_ROOT     全部 runtime 根路径指向容器内虚拟目录（/runtime/.claude 等）
#   SKILLOS_SANDBOX  =1 时为可写隔离演示模式
#
# 隔离靠文件系统边界（root 全在容器内），不靠 UI 禁用：
# scan / push / pull / 编辑 / 回滚全部可用。
#
# runtime/（预置演示数据）运行时生成，不入 git。
