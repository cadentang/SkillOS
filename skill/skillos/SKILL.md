---
name: skillos
description: Skill OS 一体化 Skill —— 一条命令把后端 + daemon + Web 控制台在本机跑起来（自带编译产物与便携 CPython，用户无需安装 Python / Node），系统跑起来后再通过 skillos_* MCP 工具查询与预览本地 Skill 资产库。当用户说「启动 skillos」「把控制台跑起来」「起一下后端和 daemon」「一键启动 Skill OS」「检测一下环境 / 装一下依赖」「把 skillos 停掉」，或「我的 Skill 装在哪了」「有哪些 Skill 过期/漂移了」「把这个 Skill 同步/分发到 Claude Code 或 Codex」「这个 Skill 是谁改的、改了什么」「跑一下 Skill 的质量检查」「把磁盘上的改动回收成新版本」时使用。写入类操作默认 dry_run 预览，冲突与删除一律交由人工在 Web 控制台决策。触发词：启动 skillos、启动控制台、起后端、起 daemon、一键启动、环境检测、安装依赖、停止 skillos、Skill 管理、Skill 漂移、Skill 同步、Skill 分发、Skill 版本、Skill 溯源、Skill 检查。
version: 1.0.0
agent_created: true
---

# Skill OS —— 一条命令把整套系统跑起来

> **本包做两件事，按顺序：**
>
> ① **把系统跑起来** —— 检测环境 → 装依赖 → 起后端 → 健康自证 → 配对 → 起 daemon（Web 控制台由后端同端口托管）；
> ② **跑起来之后管 Skill** —— 通过 `skillos_*` MCP 工具查询、检查、分发、升级本地 Skill 资产库。
>
> 后端（编译成 `.pyd` 的机器码）、前端 SPA、便携 CPython **全部就在本包内**，
> 用户机器**不需要装 Python、不需要装 Node**。双击 `start.cmd`（Windows）/ `./start.sh`（macOS·Linux）即可。

## 何时使用

**A. 启动 / 停止 / 自检**（走本包自带脚本）

- 「启动 skillos / 把控制台跑起来 / 起一下这套系统」
- 「跑之前先看看环境够不够 / 缺什么」
- 「装一下依赖」
- 「现在跑起来了没 / 什么状态」
- 「把 skillos 停掉」

**B. Skill 资产查询与管理**（系统跑起来后，走 MCP 工具）

- 「我的 Skill 都装在哪了」「一共几个」「哪些过期了 / 漂移了」
- 「把这个 Skill 同步 / 分发到 Claude Code（和 Codex）」
- 「这个 Skill 两端内容不一样，怎么回事」
- 「这个 Skill 谁改的、什么时候改的」
- 「跑一下这个 Skill 的质量检查 / 它还能用吗」
- 「把磁盘上这份改动回收成新版本」

**不要用**：与 Skill OS / 本地 Skill 资产无关的任务（写代码、查资料、跑无关测试等）。

## 三种运行形态（先分清，再动手）

本包同时服务三种发行形态，**脚本与命令完全一样**，差异只由 `manifest.json` 描述：

| | 源码形态 | **frozen 形态（对外发行默认）** | 二进制形态 |
|---|---|---|---|
| 什么时候 | 开发 / 自用 / 从仓库跑 | 从**发行包**跑（本包自带 `.pyd` 编译产物） | 从**发行包**跑（全量编译，连解释器都编进去） |
| 运行时 | 需要本机 Python ≥ 3.11 | **不需要装**（本包自带 CPython，版本与产物 ABI 一致） | **不需要**（运行时已编进二进制） |
| `bootstrap.py` | 真装依赖（建 venv + `pip install -e .`） | 真装依赖（建 venv + 按 `runtime/requirements.txt` 装第三方，**不装 skillos**） | **空操作** |
| skillos 从哪来 | venv 里的源码 | **`PYTHONPATH` → 本包 `runtime/skillos/` 的编译产物** | 编译进二进制了 |
| `--dev` | 可用 | **不支持**（编译产物无法热重载） | **不支持** |

**怎么判断当前是哪一种**：跑 `python scripts/detect.py`，第一行「发行形态」会写明。
判据是**能不能真的找到编译产物 / 可执行文件**，不是 manifest 里写了什么 —— 所以在源码仓库里不会被误判。

> **零安装入口**：包根目录有 `start.cmd`（Windows）/ `start.sh`（macOS·Linux）。
> 它们直接调用本包自带的 CPython，**用户机器完全没装 Python 也能启动**。
> 面向终端用户时优先让他们双击这个，而不是敲 `python scripts/start.py`。
>
> frozen 形态下要手敲命令的话，用**自带的**解释器最稳：
> `vendor/<平台>/python/python.exe scripts/start.py`（Windows）。
> 用系统 Python 时必须**恰好是同一个小版本**（`manifest.runtimes.python.pin`，通常是 3.11）——
> 编译产物只认这个 ABI，3.12/3.13 会被明确忽略并回落自带运行时。

## 一条铁律

**Agent 不得假报成功。** 启动结果**只以 `status.py` 的输出为准**，不以「命令已执行」为准。

判定「起来了」的硬标准（三条全中才算）：

1. 后端 `GET /api/health` 返回 `{"ok": true, "data": {"status": "up"}}`
2. daemon 进程存活（`pid_alive` 为真）
3. token 已落盘 `~/.skillos/token`

任一条不满足，就是**没起来** —— 如实报告，附日志尾部，不要把「已启动」说出口。

## 三步

### 1) 先看环境（只读，永远先跑这步）

```bash
python scripts/detect.py --json
```

看 `runtimes.python/git/node` 的 `source` 与 `version`。**检测优先**：系统达标就用系统，缺了才回落 `vendor/`。
`notes` 里有缺口提示。

### 2) 装依赖（幂等，可反复跑）

```bash
python scripts/bootstrap.py            # 常规
python scripts/bootstrap.py --dev      # 需要前端热更时（把 node 纳入必需）
python scripts/bootstrap.py --offline  # 离线机器
python scripts/bootstrap.py --mirror cn  # 国内镜像（pip 清华 + npm npmmirror）
```

装进 `<状态目录>/.venv`，**不碰系统 Python**。重复跑不会重复装。

**前端产物（单端口模式的前提）**：`bootstrap` 第 5 步按「现成 dist → 可选归档 →
node 构建」顺序解析。**发行包已内置 `runtime/web/`（`index.html` + `assets/`），
第一步即命中**；源码形态或手工补装时，可把 CI 的 `web-dist` artifact（文件名
`web-dist.zip`，zip 根即 `index.html` + `assets/`）放到包根 `vendor/` 下的 `dist`
目录，bootstrap 会自动解到包根 `web_dist/` —— **离线机、无 node 也能起单端口**。
三者都不可用时才需要 `--dev`（vite 热更）。

> **国内网络建议带 `--mirror cn`**：直连 PyPI / npm 常慢到不可用（实测直连约
> 50 KB/s，清华源可到 MB/s 量级）。预设：`cn` / `tuna` / `aliyun` / `tencent` /
> `ustc`，也可直接给 URL（`--mirror https://.../simple`），或细化到
> `--pip-index` / `--npm-registry`。优先级：CLI > 环境变量（`SKILLOS_PIP_INDEX`）
> > `manifest.json` 的 `mirrors` 块 > 官方源。

> **二进制形态下这一步是空操作** —— 脚本会打印「无需安装任何依赖」并列出四个可执行文件后返回。
> **frozen 形态下这一步装的是 `runtime/requirements.txt`（第三方），不装 skillos** ——
> skillos 是本包里的编译产物，靠 `PYTHONPATH` 加载。
> 两种形态下唯一仍需满足的硬依赖都是 **git**（真值在本地 git 仓库），缺了会非零退出并给指引。

### 3) 启动

```bash
python scripts/start.py            # 单端口：后端把 Web 一起托管，打开 http://127.0.0.1:9080
python scripts/start.py --dev      # 双端口：另起 vite，打开 http://127.0.0.1:5173
python scripts/start.py --no-daemon  # 只起后端（Web 为只读视图）
python scripts/start.py --open     # 起完自动开浏览器
```

起完**必须**跑一次 `python scripts/status.py` 复验，再向用户汇报。

## 其它命令

| 命令 | 作用 |
|---|---|
| `python scripts/status.py --json` | 只读总览：进程 / 端口 / 健康 / 运行时来源 |
| `python scripts/stop.py` | 按 PID 优雅停止（daemon → web → 后端） |
| `python scripts/vendor_fetch.py --list` | 看 vendor 运行时清单与就位状态 |
| `python scripts/vendor_fetch.py --hash <文件>` | 算 sha256（填 vendor/manifest.json 用） |

## 启动之后：用 MCP 工具管 Skill

> **本节是操作面，不是能力本体。** 本包自己不做扫描、不算哈希、不做分发 ——
> 那些能力在本包里的 `skillos` 引擎（编译产物）中，通过 MCP 工具暴露给 Agent。
> 本节的作用，是让 Agent 知道**什么时候调、调哪个、以及什么情况下必须停下来问人**。

### 前置条件

工具能调通的前提是三步，缺一步都会失败：

1. **系统已经跑起来**：后端 + daemon 在跑（见上文「三步」）。
2. **Agent 已接入 MCP**：在本机 Agent 的 MCP 配置里注册 `skillos mcp`（stdio），**指向本包自带的运行时** ——
   即用 `vendor/<平台>/python/python.exe -P -c "from skillos.mcp.server import main; main()"`，
   并把 `PYTHONPATH` 指向本包的 `runtime/skillos/`。完整配置示例见 `references/agent-config.md`。
3. **本地仓库已初始化**：首次使用需要跑一次 `init`（用本包自带运行时即可，**不需要 `pipx install`**）：

   ```bash
   # Windows（macOS/Linux 把 python.exe 换成 python）
   PYTHONPATH=<包>/runtime/skillos vendor/win-x64/python/python.exe -P -c "from skillos.cli.main import app; app()" init
   ```

> 若工具调用报「工具不存在」或连接失败，**直接告诉用户去补第 1 / 2 步，不要自行改用 CLI 或猜路径兜底**。

### 工具清单

**只读（可直接调用）**

| 工具 | 参数 | 用途 |
|---|---|---|
| `skillos_list_skills` | `category?` `tag?` `q?` | 列 Skill（按分类 / 标签 / 关键词筛） |
| `skillos_get_skill` | `slug` | 元数据 + 文件树 + `SKILL.md` 正文 |
| `skillos_status` | `slug?` | 漂移状态（7 态矩阵） |
| `skillos_check` | `slug` `version?` | 质量门禁结果 |
| `skillos_version_log` | `slug` `limit?` | 版本时间轴（谁 / 何时 / 改了什么） |

**写入（默认 `dry_run=true`，只预览不落盘）**

| 工具 | 参数 | 用途 |
|---|---|---|
| `skillos_sync_push` | `slug` `targets[]` `dry_run?` | 分发到指定 Runtime |
| `skillos_sync_pull` | `slug` `runtime` `dry_run?` | 回收磁盘改动为新版本 |
| `skillos_upgrade` | `slug` `strategy?` `dry_run?` | 上游升级 |

写入类工具**默认只预览**。先调一次让用户看清将要发生什么（尤其是 `held_back_removals` ——
将消失的路径清单），用户明确同意后，再传 `dry_run=false` 真正执行。

### 三条硬边界（不可越）

1. **会删内容的操作必须先停下**。预览返回的非空删除清单，原文念给用户并等确认 ——
   **不得为了让流程跑完而自动确认**。无人值守场景（定时、批量）遇到需要确认的删除，
   **跳过并挂起，绝不替用户决定**。
2. **冲突不自行裁决**。工具集中**没有** `force`，也没有 `on_conflict` 参数 ——
   冲突只有「中止」和「跳过」两种结局。遇到冲突，把三方状态（base / local / remote）摆给用户，
   让他在 Web 控制台做整文件四选一。**不要用 `sync_push` 反复重试来"解决"冲突。**
3. **门禁不可绕过**。`check` 报 `error` 级问题时，`sync_push` 会被拦下并返回 `GATE_FAILED`。
   **没有任何参数可以跳过门禁**。把规则明细摆给用户，由他决定在 Web 上怎么处理。

> 一句话：**Agent 能执行能力，不能执行危险决策。** 危险决策的入口只在 Web 人工路径。

### 典型对话 → 工具映射

| 用户说 | 调什么 | 注意 |
|---|---|---|
| 「我有哪些 Skill」 | `skillos_list_skills` | — |
| 「XX 装到哪了 / 什么状态」 | `skillos_status` → `skillos_get_skill` | — |
| 「把 XX 分发到 Claude Code」 | `skillos_sync_push`（先 `dry_run`） | 念删除清单再执行 |
| 「XX 两边内容不一样」 | `skillos_status` | 有冲突就停，指向 Web |
| 「XX 是谁改的」 | `skillos_version_log` | 溯源来自 git，不可篡改 |
| 「XX 还能用吗」 | `skillos_check` | `error` 级即门禁不通过 |

### 本 Skill 不做的事

- 不提供通用 git / shell 透传（不存在 `git(cmd, args)` 形式的入口）
- 不提供设备 / token 管理（撤销 token 只在 Web 人工路径）
- 不做冲突合并裁决
- 不改写版本历史（记录源是 git，应用层不提供删除 / 改写接口）

## 什么时候加什么参数

| 场景 | 参数 |
|---|---|
| 演示 / 日常使用 | 默认（单端口） |
| 改前端、要热更 | `--dev`（**仅源码形态**） |
| 只想看、不要本地写文件 | `--no-daemon` |
| 内网 / 无外网机器 | `--offline`（配合预置的 `vendor/`） |
| 端口被占 | `--port 9081` |
| 公司私有化、换账号 | `--email x@corp.com --password ******` |

## 边界（明确不做）

- **不代装系统级运行时**（Python / git / Node 本体只检测、给指引；不跑 `apt` / `winget`）。
- **不守护进程**、不设开机自启（daemon 的存活是它自己的事，本期不做）。
- **token / secret 只落 `~/.skillos/`**，绝不写进项目目录、不进 git、不进明文日志。
- 不改动 `skillos` 引擎的任何行为 —— 本包只负责「让它跑起来」，以及告诉 Agent「跑起来后怎么用」。

## 输出约定（Agent 汇报用）

启动后向用户汇报，至少给四样：

1. **控制台地址**（单端口 `http://127.0.0.1:9080` / dev `http://127.0.0.1:5173`）
2. **daemon 是否在跑**、**配对结果**（复用 / 新配对 / 失败）
3. **日志目录**（出问题让用户直接看）
4. **启动后可用的 MCP 能力**（一句：8 个 `skillos_*` 工具，查询/检查/分发/升级；写入默认 `dry_run` 预览）

## 引用资源

- `references/troubleshooting.md` —— 逐条现象 → 原因 → 处置
- `references/agent-config.md` —— Claude Code / Codex 的 MCP 接入配置（**用本包自带运行时**）与排错
- `manifest.json` —— 发行清单（端口 / 运行时要求 / 落位 / `binary` 与 `frozen` 形态映射；改这里不改脚本）
- `vendor/README.md` —— 兜底运行时的获取与离线用法
