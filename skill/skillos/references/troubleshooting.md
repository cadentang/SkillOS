# 故障排查

> 排查第一步永远是：`python scripts/status.py` + 看 `<状态目录>/logs/` 下的日志尾部。
> 状态目录默认 `~/.skillos/launcher/`（可用 `SKILLOS_LAUNCHER_STATE` 覆盖）。

## 启动类

| 现象 | 原因 | 处置 |
|---|---|---|
| `未发现可用 Python` | 系统无 ≥3.11，且 vendor 无 uv | 装 Python 3.11+；或放 `vendor/<平台>/uv`（见 vendor/README.md） |
| 创建 venv 失败 | Debian/Ubuntu 缺 `python3-venv` | `apt install python3-venv` 后重跑 bootstrap |
| `未发现 git` | git 不在 PATH | 装 git；Windows 可放 `vendor/win-x64/MinGit` 兜底 |
| bootstrap 装包超时 | 网络慢 / 走了境外源 | **先加 `--mirror cn`**（pip 清华 + npm npmmirror）；或细调 `--pip-index` / `--npm-registry`；或 `--offline` + 预置 wheel（`vendor/wheel/`） |
| 后端起后健康检查失败 | 依赖缺失 / 端口冲突 / 代码报错 | 看 `logs/backend.log` 尾部，脚本已自动打印 |
| `端口 9080 已被占用` | 残留进程或他人服务 | `python scripts/stop.py`；或 `--port 9081` |
| daemon 拉起即退出 | 后端未就绪 / 配对码无效 | 先确认 `status.py` 里后端 healthy；看 `logs/daemon.log` |
| vite 起不来（--dev） | 缺 node 或 5173 被占 | `bootstrap.py --dev`；或去掉 `--dev` 用单端口 |

## 运行时相关

| 现象 | 原因 | 处置 |
|---|---|---|
| 明明装了 Python 却走了 vendor | 系统版本低于下限 | `detect.py --json` 看 `source`；换 `--python` 指向也可，或升级系统 Python |
| `uv python install` 失败 | 离线 / 网络不通 | 联网后重跑；离线机器改用 vendor 预置 CPython |
| git 用了 MinGit 但命令报 `GIT_EXEC_PATH` | MinGit 布局不含 exec-path | 确认 `vendor/<平台>/MinGit/mingw64/libexec/git-core` 存在 |

## 状态 / 凭据

| 现象 | 原因 | 处置 |
|---|---|---|
| 重启后「全部 401」 | JWT secret 变了 | 本启动器已在 `<状态目录>/secret` 固定；确认该文件存在且未被删 |
| 每次启动都要重新配对 | token 未落盘 / 中途清了 `~/.skillos` | 确认 `~/.skillos/token` 存在且可写 |
| `status.py` 显示 daemon 未运行但进程在 | PID 被复用 / 手工起过 | `stop.py` 收干净后重新 `start.py` |
| 想彻底重置 | —— | `stop.py` → 删 `~/.skillos/launcher/state.json` 与 `~/.skillos/cloud.db` → 重新 `bootstrap.py` + `start.py`（**不会**动 Skill 仓库） |

## frozen 形态（编译发行包，默认；见 docs/10）

症状前缀对不上时先跑 `python scripts/detect.py` 看第一行「发行形态」。

| 现象 | 原因 | 处置 |
|---|---|---|
| `系统 Python 3.12/3.13 已忽略：发行物锁定 CPython 3.11` | **正常行为**。编译产物是 `cp311-*.pyd`，只认 3.11 的 ABI | 不用管 —— 会自动回落到发行物自带的 CPython。想用系统 Python 就得装 3.11.x |
| 用户机器完全没有 Python | —— | 用发行包根目录的 **`start.cmd`**（Windows）/ **`start.sh`**（macOS·Linux），它们直接调自带的解释器 |
| `这是 frozen（编译发行）形态，但找不到依赖清单 requirements.txt` | 发行包不完整 | 重新解压 / 重新构建；确认 `<包>/runtime/requirements.txt` 存在 |
| `ModuleNotFoundError: No module named 'skillos'` | `PYTHONPATH` 没接上（多半是手工用了 `-I` 或自己造了环境变量） | 用 `start.py` 启动（它会注入 `PYTHONPATH`）；手工调试时用 `-P` 而不是 `-I` |
| `ImportError: Module 'X' was actively excluded from Nuitka compilation` | 该第三方依赖没装上（`requirements.txt` 被裁剪过） | 重新 `bootstrap.py --force`；或核对 `requirements.txt` 条目数 |
| 依赖装到一半失败 | 网络慢 / 走了境外源 | `bootstrap.py --mirror cn` |
| 以后端/daemon 起不来、日志里是 `skillos/` 路径相关错误 | `<包>/runtime/skillos/` 被移动或删了 | 重新解压；该目录（`.pyd` + 数据目录）必须在包内 `runtime/` 下 |

## 二进制形态（全量编译发行包；见 docs/10）

| 现象 | 原因 | 处置 |
|---|---|---|
| `bootstrap.py` 秒退、说"无需安装任何依赖" | 正常 —— 二进制形态下运行时已编译进发行物 | 不用管，直接 `start.py` |
| `找不到 server 可执行文件。发行包可能不完整` | `runtime/skillos/` 目录被删或未完整解压 | 重新解压发行包；确认 `runtime/skillos/skillos-server(.exe)` 存在且可执行（POSIX 需 `chmod +x`） |
| `二进制形态不支持 --dev` | 编译产物无法热重载 | 改前端请回到源码仓库跑 `start.py --dev`；或改完重新 `npm run build` 后重新构建发行物 |
| 后端起不来、日志空白 | 二进制被安全软件拦截 / 未授予执行权限 | 看 `logs/backend.log`；Windows 检查杀软隔离区；POSIX `chmod +x runtime/skillos/*` |
| `/` 返回 404 而 `/api/health` 正常 | `runtime/web/` 未随包解压，或 `SKILLOS_WEB_DIST` 指错 | 确认 `<包>/runtime/web/index.html` 存在 |
| 提示找不到 `SKILLOS_PORT` | 你手工起了二进制而没传环境变量 | 手工起后端时设 `SKILLOS_HOST` / `SKILLOS_PORT`（**端口走环境变量，不是命令行参数**） |
| macOS 报"已损坏，无法打开" | 未做代码签名 / 公证 | 本期未处理；临时 `xattr -dr com.apple.quarantine <目录>`，正式分发前需公证 |

## 安全相关（不要做）

- 不要把 `~/.skillos/launcher/secret` 或 `~/.skillos/token` 拷进项目目录（NFR-2）。
- 不要在自动化里替用户确认删除类操作 —— 本包只在 MCP 工具里**预览** Skill 变更，不替用户裁决冲突与删除。
- `vendor_fetch.py` 默认拒绝安装 sha256 未锁定的产物；不要习惯性加 `--allow-unverified`。
