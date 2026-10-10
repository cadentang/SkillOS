# skillos

Skill OS 的**一体化 Skill 包**：一条命令把后端 + daemon + Web 控制台在裸机上拉起来，
并告诉 Agent 跑起来之后怎么用 MCP 工具管 Skill。

**包里带着编译产物**（后端 `.pyd` / 前端 SPA / 便携 CPython），所以终端用户
**不需要装 Python、不需要装 Node** —— 双击 `start.cmd` / `./start.sh` 即可。

## 目录

```
skillos/
├── SKILL.md                  # 主文件：启动三步 + 铁律 + 启动后可用 MCP 工具 + 三条硬边界
├── manifest.json             # 发行清单：运行时要求、端口、落位（改这里不改脚本）
├── scripts/                  # 全部纯标准库，依赖装好前即可运行
│   ├── _common.py            #   公共库（含 frozen / 二进制形态识别）
│   ├── detect.py             #   只读：环境探测（第一行报发行形态）
│   ├── bootstrap.py          #   幂等：建 venv + 装依赖
│   ├── start.py              #   编排：后端 → 健康 → 配对 → daemon → web
│   ├── stop.py               #   优雅停止
│   ├── status.py             #   只读：运行状态
│   └── vendor_fetch.py       #   vendor 运行时下载 / 校验
├── references/
│   ├── troubleshooting.md    #   逐条现象 → 原因 → 处置
│   └── agent-config.md       #   Claude Code / Codex 的 MCP 接入配置
├── vendor/                   # 兜底运行时（仓库内为空；发行包内放便携 CPython / MinGit）
├── runtime/                  # ★ 编译载荷（构建期注入，不入 git）
│   ├── skillos/              #   skillos.cp311-win_amd64.pyd + 数据树（PYTHONPATH 注入点）
│   ├── web/                  #   前端预构建产物（无 sourcemap）
│   └── requirements.txt      #   第三方依赖清单
├── start.cmd / start.sh      # 零安装入口（构建期生成）
└── BUILD-INFO.json           #   版本 / 校验和 / 源码泄漏自检（构建期生成）
```

> 仓库里只有上面**不带 ★ 的部分**（源码形态）；`runtime/`、`vendor/<平台>/`、`start.*`、
> `BUILD-INFO.json` 全部由 `build/build.py` 在装配时注入（见仓库 `docs/10-发行与编译.md`）。

## 快速开始

**终端用户**（拿到发行包）：双击 `start.cmd`（Windows）/ `./start.sh`（macOS·Linux）→ 打开提示的地址。

**开发 / 从仓库跑**：

```bash
python scripts/detect.py      # 1. 看环境
python scripts/bootstrap.py   # 2. 装依赖（幂等）—— 国内建议加 --mirror cn
python scripts/start.py       # 3. 启动 → http://127.0.0.1:9080
python scripts/status.py      # 4. 复验
python scripts/stop.py        # 收工
```

> 国内网络：`python scripts/bootstrap.py --mirror cn`（pip 走清华、npm 走 npmmirror）。
> 也可用 `tuna` / `aliyun` / `tencent` / `ustc` 预设或直接给 URL。

## 设计要点

- **检测优先**：系统已有且在版本范围内的 Python / git / Node 直接用；`vendor/` 只是兜底层。
- **零 Node**：Web 走预构建产物（`runtime/web`），由后端同端口托管；Node 只在 `--dev` 热更时需要。
- **纯标准库脚本**：保证在「依赖还没装」的那一刻也能运行。
- **状态隔离**：venv / secret / PID / 日志全落 `~/.skillos/launcher/`，不污染系统，不写进项目目录。
- **只信实物**：判发行形态看「能不能真的找到 `.pyd` / 可执行文件」，不看 manifest 写了什么 ——
  所以在源码仓库里永远不会被误判。

## 三种形态

| | 源码形态 | **frozen 形态（发行包默认）** | 二进制形态 |
|---|---|---|---|
| 适用 | 开发 / 自用 / 从仓库跑 | 对外分发（源码不可见 + 零安装） | 对外分发（连运行时都不带） |
| 需要 Python | 是（≥ 3.11） | **否**（包内自带同版本 CPython） | **否**（运行时已编译进发行物） |
| `bootstrap.py` | 建 venv + `pip install -e .` | 建 venv + 按 `runtime/requirements.txt` 装第三方 | 空操作（明确告知无需安装） |
| skillos 从哪来 | venv 里的源码 | `PYTHONPATH` → `runtime/skillos/*.pyd` | 编译进二进制 |
| `--dev` 热更 | 支持 | **不支持** | **不支持** |
| 产物来源 | 本仓库 | `build/build.py`（`route=module`，见 `docs/10`） | `build/build.py`（`route=standalone`） |

判定方式：`python scripts/detect.py` 第一行「发行形态」。

脚本**三种形态共用**，一行都不用改；切形态只改 `manifest.json`（对外发行时由 `build.py` 自动生成）。

```jsonc
// frozen 形态：编译产物目录 + 第三方清单 + ABI 锁定的 Python 版本
"frozen": {
  "dir": "runtime/skillos",            // PYTHONPATH 注入点（.pyd 在这里），相对本包根
  "requirements": "runtime/requirements.txt",
  "python_tag": "cp311",
  "python_pin": "3.11"
}

// 二进制形态：角色 → 可执行文件（dir 相对本包根，故整目录可搬移）
"binary": {
  "roles": {
    "cli":    { "dir": "runtime/skillos", "name": "skillos" },
    "mcp":    { "dir": "runtime/skillos", "name": "skillos-mcp" },
    "server": { "dir": "runtime/skillos", "name": "skillos-server" },
    "daemon": { "dir": "runtime/skillos", "name": "skillos-daemon" }
  }
}
```

### frozen 形态的两个要点

1. **`python.pin` 是硬约束**：`.pyd` 只认编译它的那个 CPython 小版本。系统有 3.12/3.13
   也**不能用** —— `resolve_runtimes()` 会在 notes 里说明并回落到包内自带的运行时。
2. **探测必须用 `-P` 而不是 `-I`**：`-I` 隐含 `-E`，会把 `PYTHONPATH` 一并忽略 ——
   而 frozen 的 skillos 只能靠 `PYTHONPATH` 找到（它不在 venv 里），用 `-I` 会永远判「没就绪」。

### 零安装入口

包根目录的 `start.cmd`（Windows）/ `start.sh`（macOS·Linux）直接调用包内自带的 CPython，
**用户机器完全没装 Python 也能启动**。面向终端用户时让他们用这个。
