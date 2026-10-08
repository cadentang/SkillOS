# sandbox/ —— 沙盒 Runtime（FR-2.4.3 / FR-3.10）

> 本文只讲**沙盒这一块**的细节；完整部署流程（线上 compose / 本地三件套 / 排障）见 [`../docs/09-部署手册.md`](../docs/09-部署手册.md)。

沙盒与云端共用同一镜像（`../Dockerfile`），仅环境变量切换：

| 变量 | 作用 |
|---|---|
| `SKILLOS_ROOT` | home 指向容器内虚拟目录（`/runtime`），runtime / repo / db 全在容器内 |
| `SKILLOS_SANDBOX=1` | 可写隔离演示模式：挂载 `POST /api/sandbox/reset`，`health` 暴露 sandbox 标志 |

隔离靠文件系统边界（root 全在容器内），不靠 UI 禁用：
scan / push / pull / 编辑 / 回滚全部可用（04 §2.7.7）。

## 部署（compose 两容器，04 §1.4）

```sh
echo "SKILLOS_JWT_SECRET=$(openssl rand -hex 32)" > .env
docker compose up -d --build
# 评委入口: http://<host>:8000（demo@skillos.local / skillos-demo）
# 沙盒 daemon 连 cloud 容器，所有写操作落在 sandbox-data 卷内
```

## 文件

- `entrypoint-sandbox.sh` —— 沙盒容器入口：install-seed（幂等）→ 配对 → daemon 前台
- `../scripts/build_sandbox_seed.py` —— 生成 seed（3 skill × 4 端，对齐 demo 剧本）
- `../scripts/sandbox_bootstrap.py` —— install-seed / pair 两个子命令（纯标准库）

## seed 语义

- seed 是 reset 的**恢复源**（`<root>/.skillos-seed/runtime/<runtime_key>/`）
- reset 后的 runtime 字节是 push 渲染产物（`render_layout` 按各端字段策略重建），
  与产品正常分发行为一致 —— seed 只是源材料
- seed 集合不得含 error 级门禁违规：reset 会 push 全部 skill，门禁拦截会让重置失败
- 重置走 `POST /api/sandbox/reset`（三层可达检查：engine.is_sandbox / daemon 分支 / API 404）；
  runtime root 越出容器根时拒绝执行（BOUNDARY，不删任何内容）

`runtime/`（运行时状态）在卷内生成，不入 git。
