# CODEBUDDY.md — Skill OS 项目规则

> 🔒 **已冻结 · 开发基线 BASELINE-1.0**（2026-10-06）—— `docs/` 与本文档同为**开发最终版本**，实现以此为准。文档索引与变更流程见 `docs/README.md`。
>
> 本文件位于项目根目录，CodeBuddy 会在每次会话开始时**全文加载**。
>
> **第一原则：代码不得直接从聊天记录生成。**
> 动任何代码之前，先读 `docs/` 下的设计文档，按文档写；不以对话记忆为依据。
> 文档没写到的地方，先问，不要猜着实现。

---

## 0. 项目一句话

Skill OS —— 个人 AI Skill 操作系统。本地 Git 仓库是 canonical（SSOT），通过 adapter 分发到 `.claude` / `.agents` / `.workbuddy` / `.codex` 四个 Runtime。

**云端保留写路径职责**（元数据 / 协作 / Web UI / 任务下发 / `sync_records` 双写），但**不存文件内容**（`FR-3.12`）、**不判冲突**（冲突在本地三方比对，云端只转发）—— **真值始终在本地 git + 磁盘**（`NFR-1`）。术语「数据面」的两个用法见 `docs/04-系统设计.md` §1.2。

**交付物只有 3 个**：`skillos` Python 包（CLI / daemon / MCP 三种运行模式，**同一个包**）、Web 控制台、云端后端。
约束：单人开发 / 18 天 / 线上可访问 / 赛后长期自用。

---

## 1. 动手前必读（强制流程）

| 顺序 | 文档 | 何时必读 |
|---|---|---|
| 0 | `docs/README.md` | **总是**。文档索引、冻结基线声明、变更流程 |
| 1 | `docs/00-需求定稿.md` | **总是**。权威需求源，`FR-*` / `NFR-*` / `OUT-*` 编号全部引用自它 |
| 2 | `docs/02-技术设计.md` | **总是**。技术选型基线：§9 选型表、决策 **T1–T22** |
| 3 | `docs/04-系统设计.md` | 改模块 / 数据库 / 接口 / 时序 / 前端时 |
| 4 | `docs/05-跨平台兼容性.md` | 涉及路径、文件系统、编码、Windows / macOS / Linux 差异时（**P0 项无一可省**） |
| 5 | `docs/03-开发排期.md` | 需要确认当前阶段与验收口径时 |
| 6 | `docs/06` / `docs/07` | 想改某个"看起来多余"的约束时 —— **先查它是不是踩坑换来的**（18 项采纳 + 30 条坑，多数带上游 issue 号） |
| 7 | `app/index.html`、`Skill OS 设计系统.pdf` | 动前端 UI / 颜色 / 组件样式时 |
| 8 | `docs/09-部署手册.md` | 改 `Dockerfile` / `compose.yaml` / API 入口 / 静态托管 / 环境变量 / 沙盒入口脚本时 |

**裁决顺序（文档冲突时）**

- 需求口径冲突 → 以 `00-需求定稿.md` 为准
- 技术选型冲突 → 以 `02-技术设计.md` §9 / §10.1（决策 **T1–T22**）为准
- 架构 / 模块 / 数据模型冲突 → `04` → 然后 `05`
- `01-需求讨论稿.md` **仅作背景，不作依据**

**`OUT-*` 是明确的「不做」清单** —— 动手前先查一遍，别实现一个已被否决的方案。

---

## 2. 技术栈速查

选型论证见 `02-技术设计.md` §9 与 §10.1；前端落地约定见 `04-系统设计.md` §2.7.1。

| 层 | 选型 |
|---|---|
| 引擎 / CLI / daemon / MCP | Python 3.13 + Typer + 官方 MCP SDK |
| API / 实时 | FastAPI + Uvicorn（**与 WebSocket 同进程**） |
| ORM / 迁移 | SQLAlchemy 2.x + Alembic |
| 数据库 | 本地 SQLite、云端 Postgres（**表结构同构**） |
| 版本控制 | Git（subprocess 调用，不自研 VCS） |
| 前端 | React + Vite + TypeScript（**不用 Next.js**） |
| 样式 | Tailwind + shadcn 风格组件 |
| 编辑器 | Monaco（**异步加载**，不进主 bundle） |
| 前端状态 | TanStack Query + WS 事件驱动失效 |
| API 类型 | `openapi-typescript` → `web/src/api/schema.d.ts`（提交进 git，**禁止手改**） |
| 部署 | 容器（云端）+ 本地 pipx；单仓 monorepo：`skillos/` + `web/` + `sandbox/` |

---

## 3. 工程红线（不得违反）

1. **分层方向单向**：`入口（CLI / daemon / MCP / API） → services → core / store`。
   `core` **不得** import `services` 或 `store`。
2. **`core/fs` 的文件操作必须显式接收 `root: Path`**，不得从全局配置 / 单例 / 环境变量隐式读取（决策 T1）。沙盒靠把 `root` 指向容器内虚拟目录实现隔离。
3. **不引入 VFS / Storage Provider 抽象层**（T1）。路径校验（`resolved_path` 必须落在 `root` 内）在 `core/fs` 统一实现，不散落各处。
4. **不提供通用命令透传**（OUT-14）：不允许 `run(cmd, args)` 形式的通用 git / shell 透传。只暴露**具名领域操作**（`init` / `commit` / `log` / `show` / `snapshot_at` / `diff` / `rollback`）—— **任务表即白名单**。
5. **云端不碰磁盘**：`scan` / `drift` / `distribute` 只在本地执行。云端漂移状态由 daemon 上报后落库。
6. **云端不缓存文件内容**（FR-3.12）：`GET /api/skills/{id}/file` 一律下发 `file_read` 任务实时取回。
7. **写接口强制 `base_hash` 乐观锁**（NFR-3.1）：不匹配返回 `409 STALE`，提示重新加载，**绝不静默覆盖**。
8. **`render_layout()` 必须是纯函数**（T6 / T7）：同一 `SkillSnapshot` + 同一 `adapter.version` 调用两次，输出逐字节相同。改 adapter 的字段策略 / sidecar 模板 / 目录布局 ⇒ **必须 bump `adapter.version`**。
9. **base 现算不落盘**（T6）：由 `installed_commit` + `adapter_version` 重建。`installed_version` **仅展示用，不作任何基线**。不要新建 `runtime_binding_files` 表。
10. **漂移状态是缓存，不是事实源**：`runtime_bindings.status` 任何时候都必须能由「磁盘 + canonical commit + adapter 版本」重算。
11. **大文件策略**（T2 / FR-1.0.20）：**判定以单文件为单位**（超阈值默认 5MB，常量集中在 `core/constants.py`）⇒ `in_git = false`、**不参与漂移哈希**；**聚合与徽标以 Skill 为单位** —— 含此类资产的 Skill **禁止显示 `SYNCED`**，改显示 `SYNCED*` + 徽标。分发时必须回收未纳管资产，不得静默删除。
12. **冲突不做块内合并**（第一版）：**整文件粒度四选一**（保留本地 / 保留仓库 / **两个都保留** / 暂不处理），不做 diff3 标记解析与逐块交互。**任一选择前必须先建安全快照且可撤销**（FR-2.2.6 / T22）；「两个都保留」只在 Web 人工路径，**MCP 工具参数中不得出现 `keep_both`**。
13. **MCP 工具只读优先，写工具必须带 `dry_run`**；**不暴露 `force`**、**不暴露通用 git / shell**、无设备管理工具。危险决策只留人工路径。
14. **沙盒下不隐藏写操作**（FR-2.4.3）：隔离靠文件系统边界（root 全在容器内），不靠 UI 禁用。`scan` / `push` / `pull` / 编辑 / 回滚全部可用。

**以下 7 条是 v1.3 / v1.4 新增的安全红线（每条都对应上游真实数据丢失或静默失效事故，不得简化）**：

15. **写入授权不可省略**（FR-1.0.26 / T13）：`ReplacePolicy` = `NO_CLOBBER` / `RECORDED` / `USER_CONFIRMED`，**无默认值**，**每个写入调用点必须显式声明**。不得为了方便给默认值。
16. **会删内容必须先停下**（FR-1.0.26 / T14）：任何要移除的路径**必须先列出并等确认**；比较对象是**暂存树**而非原目录；**无法判断存在与否时报错，不许猜**；无人值守路径（MCP / daemon 定时）一律跳过而非静默执行。
17. **部署自证**（FR-1.0.27 / T14）：写完 runtime 目录后**必须回读目标**、核对清单与哈希，一致才允许上报成功。"我写成功了"与"它在那"是两件事。
18. **跨目标整体拒绝**（FR-1.0.28 / T15）：同一 Skill 推多个 Runtime 时，**任一 binding 非 `SYNCED` 即整体拒绝**，不接受"部分成功"。`SYNCED` 是**唯一可作证明**的状态，其余六态一律不可。**代码层面：内层 Runtime 循环体内不得出现 `break` / `continue`**（外层 Skill 循环允许）。
19. **哈希两级，不可合并成一个函数**（FR-1.0.5 / T16）：`hash_bytes` **字节级、绝不归一化**（存储身份）；行尾 / NFC / 尾随空白只在 `hash_bytes` 已判定不等时作**第二级兜底比较**。第二级只折叠文本、**孤立 `\r` 不动**、二进制不折叠。禁止读 `updated_at` 之类的 DB 列参与新鲜度判定。
20. **锁与网络分离**（FR-1.0.17 / T17）：网络 I/O **永不在锁内**；**禁止持锁 `fsync` / 全量目录 rename / 更新目录 mtime**；抢锁失败**不按异常类型分支**（Windows `ERROR_LOCK_VIOLATION` 不映射 `WouldBlock`，必须重试任何错误）；前后台锁分级（前台等待 20s）。
21. **路径即数据 + 路径键归一化**（FR-1.0.25 / FR-1.0.31 / T12 / T21）：`adapters/` 中**不得出现字面量路径**，新增 Runtime 只加数据不加代码；**部署根（`deploy_dir`）≠ 发现根（`scan_only_dirs`）**，二者永不混用；路径去重 / 冲突检测**必须走单一 `path_key()`**（分段 + NFC + casefold），`path_key` **不得**替换用户可见的真实路径。
22. **协议协商 + 元数据版本化**（FR-1.0.29 / FR-1.0.30 / T18 / T19）：commit trailer `Skillos-Protocol: N` + sticky `protocol.json` **双向**校验，"有文件无 trailer" = 旧客户端 → **拒绝写入**；协作元数据进仓库 `.skillos/`（SQLite 仅缓存、可重建），**元数据说 0 个 Skill 而仓库有目录 ⇒ 抛 `META_INDEX_MISMATCH`，不得重建**（首次克隆走全量扫描，不走 `rebuild_index`）。

---

## 4. 跨平台 P0（写代码前必须落地）

详见 `docs/05-跨平台兼容性.md`。以下五条一律不得绕过：

1. **换行口径统一在「字节」这一侧**（FR-1.0.5 / T16）：`hash_bytes` **字节级、不归一化**，行尾归一化只在**第二级兜底比较**里做；git 侧靠 `.gitattributes` 的 `* -text` + 显式 `-c core.autocrlf=false -c core.quotepath=false` 保证 **checkout 不改字节**。两侧必须一致在"字节"上 —— 旧写法"哈希归一化、git 不归一化"会让 `git status` 永远显示文件被改、漂移检测持续误报。
2. **`__pycache__/` 与 `*.pyc` 必须进哈希排除集**（FR-1.0.5）：Skill 里的 Python 脚本一运行就生成 pyc，不排除则**每次使用后部署副本永久显示"已变更"**。枚举集单一只认 `list_content_files()`。
3. **中文 + Windows 控制台编码**：本机 62 个 skill 描述全是中文，CLI 输出前必须显式处理编码，否则一输出就崩。
4. **路径大小写与非法字符 + Unicode 归一化**：跨平台路径处理不得依赖平台默认行为；路径比较走 `path_key()`（分段 + **NFC** + casefold），**只比大小写会漏掉中文名的 NFC / NFD 差异**。
5. **repo 不得放在云同步目录里**；**WSL 与 Windows 文件系统边界**不得跨越。

> 崩溃是好事（立刻发现），**持续误报才是地狱**。P0 项无一可省。

---

## 5. 前端约定

- **浏览器不得直连数据库**（含任何 BaaS / SDK 形式的库直连）。所有数据走云端后端 REST / WS；**文件内容必须经 daemon 代理实时取回**。
- **不引入 SSR / Serverless 运行时**：daemon 依赖常驻 WebSocket 长连接（`/ws/agent`），Serverless / Edge 承载不了。
- **设计令牌唯一来源**：`app/index.html` 的 `:root` Design Tokens 段。漂移 7 态采用**色 + 形双编码**，令牌名 `--drift-*` 已落定，**不得改名、不得改色值**。
- **Monaco 必须动态 `import()`**，且 demo 主线（Dashboard + 漂移 + 一键分发）**不得依赖它**。
- **接口变更后的固定动作**：`npm run gen:api` → 看 TS 报错 → 修调用点。**禁止手改 `schema.d.ts`**。

---

## 6. 编码风格

- **Python**：类型注解必写。内部结构用 `@dataclass` / Pydantic，不用裸 `dict` 传递。
- **TypeScript**：不用 `any`。类型从 `schema.d.ts` 取，用索引类型如 `paths['/api/skills']['get']`。
- **注释写「为什么」，不写「做了什么」**。决策类注释标编号，例如 `# T1: root 必须由调用方显式传入`。
- 命名：Python 文件蛇形；React 组件帕斯卡。

---

## 7. 禁止事项

- ❌ **报错静默兜底**（"取不到就给默认值"）。失败要显式抛出或上报，不许糊过去。
- ❌ 实现 `00-需求定稿.md` 中 `OUT-*` 已明确不做的东西。
- ❌ 在 `core` 里 import `services` / `store`。
- ❌ 新增依赖前不查 `02` §9 是否已有既定选型。
- ❌ 手改 `web/src/api/schema.d.ts`。
- ❌ 绕过 `base_hash` 校验直接写文件。
- ❌ 让 `core/fs` 从环境变量隐式取路径。
- ❌ **给 `ReplacePolicy` 加默认值**（T13）；❌ 在 removals 守卫里"猜"文件是否存在 —— 判断不了就报错。
- ❌ **在 `with lock:` 块内做网络 I/O / `fsync` / 全量目录 rename / 更新目录 mtime**（T17）。
- ❌ **用 `compare_normalized()` 的结果参与漂移状态判定**（T16）—— 它只用于"是否仅风格差异"的提示。
- ❌ **在 `adapters/` 里写字面量路径**（如 `".claude"`）（T12）；新增 Runtime 只加数据、不加代码。
- ❌ **内层 Runtime 循环用 `break` / `continue`**（T15）—— 必须整体拒绝。
- ❌ **手改 `.skillos/` 下的协作元数据**（T19）—— 它是仓库的一部分，走代码路径写入。

---

## 8. 工作方式

1. **先读文档 → 再给方案 → 确认后写代码。**
2. 有 2 个以上合理路径时，先摆选项与取舍，不要替用户决定。
3. 需求不清时先问，不要猜着实现。
4. 报错给**根因**，不要只给现象。
5. 改动完成后说明三件事：**改了哪些文件 / 为什么 / 有无未决风险**。
6. 设计文档也要同步更新 —— 代码与 `docs/` 口径不一致时，一并修正，不要留成两份真相。
