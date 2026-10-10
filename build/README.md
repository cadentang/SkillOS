# build/ —— 编译发行管线

把 `skillos` 编译成原生机器码，产出一份「使用者看不到源码、且不需要装任何东西」的发行包。
设计口径见 **`docs/10-发行与编译.md`**；本文件只讲**怎么用**。

## 30 秒上手

```bash
# 只想看看会执行什么命令（无编译器也能跑）
python build/build.py --dry-run

# 真构建。首次约 11 分钟（Nuitka 缺编译器时会自动下载 MinGW64）；
# 之后 ccache 命中，通常 5 分钟内。
python build/build.py --vendor-python /path/to/cpython   # 指定随包的便携 CPython（可选）

# 对产物做四重自证 —— 别跳过
python build/smoke.py --mirror cn
```

产物在 `build/dist/skillos-<platform>/`（约 86MB）—— **整目录就是一个可交付的 skill 包**
（根有 `SKILL.md`，载荷在 `runtime/` 下），并附带 `skillos-<version>-<platform>.zip`。
想落到别处：`--out-dir /交付/skillos`。

> **构建机必须是 Python 3.11** —— 产物是 `cp311-*.pyd`，只认同一 ABI。
> `build.py` 会在开始前拦住其他版本并给出三条出路。

## 常用参数

| 参数 | 作用 |
|---|---|
| `--list` | 列出当前路线与规格，不构建 |
| `--dry-run` | 只打印命令。**改 `nuitka.json` 后先跑这个**核对参数 |
| `--route module\|standalone` | 覆盖规格里的路线。`module` 是默认（快、第三方走 venv）；`standalone` 是退路（自足但冷构建 ~2.5h） |
| `--vendor-python <目录>` | 要随包的便携 CPython 目录（含 `python.exe` 或其一级子目录） |
| `--out-dir <目录>` | 把装配好的 skill 包输出到该目录（默认 `build/dist/skillos-<平台>`） |
| `--clean` | 先清 `build/out` 与 `build/dist` |
| `--target <name>` | （仅 standalone）只构建某个 target |
| `--skip-build` | 复用 `build/out`，只重新装配发行目录（改装配逻辑时省一次全量编译） |
| `--compiler auto\|mingw64\|msvc` | Windows 编译器选择。3.11 下默认 `mingw64`（免装 VS） |
| `--version-string v1.2.3` | 覆盖产物版本号（会自动规整成 `1.2.3`） |
| `--no-archive` | 不生成 zip |

`smoke.py` 另有 `--dist <目录>`、`--mirror <index>`（默认清华源，`none` 走官方）、
`--rebuild-venv`（强制重建冒烟 venv）、`--port <端口>`（默认 9288）、`--keep`（保留临时环境便于排查）。

## 目录约定

```text
build/
├── nuitka.json        ★ 唯一真相。选路线 / 加库 / 改平台分支，只改这里
├── build.py             驱动：组装命令 → 执行 → 装配 → BUILD-INFO.json
├── smoke.py             产物四重冒烟（形态保密 / 运行时 / CLI / MCP / ASGI）
├── entrypoints/         编译入口（**仅 standalone 路线使用**）
├── out/                 中间物：Nuitka 的产物与 .report.xml（可安全删除）
└── dist/                最终物：`skillos-<平台>/`（一个完整 skill 包）+ zip（可安全删除）
```

`build/out` 与 `build/dist` 都是可再生成的中间/最终物，**不入 git**。

交付包的形态（`docs/10` §2.2）：

```text
build/dist/skillos-win-x64/          ← 整目录 = 那个 skill 包，压缩即交付
├── SKILL.md  manifest.json  README.md
├── scripts/  references/            ← 启动器脚本与文档
├── runtime/skillos/                 ← 编译产物（.pyd + 数据树），PYTHONPATH 注入点
├── runtime/web/  runtime/requirements.txt
├── vendor/win-x64/python/           ← 便携 CPython
└── start.cmd  start.sh              ← 零安装入口
```

## 两条路线

| 路线 | 编译什么 | 产物 | 用户侧 |
|---|---|---|---|
| **module**（默认，D1） | 只编 `skillos/` 的 76 个模块，第三方按 `site-packages` 扫描自动 `--nofollow-import-to` | `skillos.cp311-win_amd64.pyd` 4.5MB | 随包 venv 装第三方 + 随包便携 CPython → 零安装 |
| standalone（退路，A） | 连第三方一起编（1637 个模块 / 941MB C 源码） | 自足可执行目录 ~150MB | 什么都不用装，但冷构建约 2.5 小时 |

切换：改 `nuitka.json` 顶层 `route`，或 `--route standalone`。

## 改配置的正确姿势

1. 改 `build/nuitka.json`（**不要**改 `build.py` 里的 include 列表 —— 那里没有）
2. `python build/build.py --dry-run` 核对命令
3. `python build/build.py` 真构建
4. `python build/smoke.py` 验证

**加一个新依赖时**（module 路线）：**通常什么都不用做** —— `build.py::collect_exclude_packages()`
会自动把 site-packages 里的新顶层包挡在编译面外，第三方由 venv 在运行期提供。
只有当你**希望把它编进机器码**时，才需要动规格。

## 运行时依赖清单从哪来

`nuitka.json` 的 `runtime_requirements.source` 决定 `requirements.txt` 怎么生成（frozen 形态下随包下发给 launcher，用户首次启动时装它）：

| 取值 | 做法 | 适用 |
|---|---|---|
| **`pyproject`**（默认） | 直接抄 `pyproject.toml` 的 `[project].dependencies`，**刻意不 pin** | 常规。版本交给区间约束 + pip 解析 |
| `freeze` | 取构建 venv 的 `pip freeze`，减去 `freeze_exclude` 里的构建期工具 | 需要可复现构建时 |

**为什么默认不 pin**：`pip freeze` 会连构建机上的**偶然版本**一起固化。实测冻出过 `attrs==26.1.0`，而 PyPI 上最高只有 `25.3.0` —— 用户装到一半必然 `No matching distribution found`。这类故障只在「全新环境」暴露，构建机的老 venv 永远看不见。

生成后有一道健全性闸门：`runtime_requirements.must_contain` 列的包名必须都在清单里，否则**构建期直接失败**（而不是等用户装完才发现跑不起来）。

（standalone 路线）先判断它是否「按字符串加载模块」：是 → 加进 `standalone.include_packages`
并写上 `why`；否 → 通常不用管，Nuitka 会静态跟随。判据见 `docs/10` §4。

## 出问题时的排查顺序

| 症状 | 大概率原因 | 处置 |
|---|---|---|
| 报构建机 Python 版本不符 | route=module 要求**恰好 3.11**（`.pyd` 的 ABI 锁） | 换 3.11 构建环境；或 `--route standalone`；或交给 CI |
| `ModuleNotFoundError: No module named 'skillos.xxx'` | 误用了 `--mode=module`（只编单模块，子模块不进去） | 用 `--mode=package`（规格已写死） |
| 运行期 `ImportError: Module 'X' was actively excluded ...` | Nuitka 的部署安全阀 | 必须带 `--no-deployment-flag=excluded-module-usage`（规格已写死） |
| 跑到某功能才报「文件不存在」 | 数据文件没落位 | `--include-data-files` 在 package 模式**不落盘**，靠 `copy_data_files()` 按 `<输出目录>/<dest>` 显式拷。检查 `include.data_files` 的 `dest` 是否保持包内相对路径 |
| `import skillos` 拿到了源码而不是 `.pyd` | `<dist>/runtime/skillos/` 下有残留 `.py` | `smoke.py` 第 0 步会拦住；清掉重新装配 |
| 便携 CPython 体积翻倍（76MB→150MB） | `uv python install` 会额外放一个**符号链接**，`copytree` 跟随了它 | 已由 `_python_install_root()` 处理（只拷实体安装根） |
| `--skip-build` 之后发行目录**丢了便携 CPython** | `assemble_module()` 会先 `rmtree(<dist>)` 再重建 —— 连带删掉上一份随包运行时 | 装配前必须能再拿到一份便携 CPython：预置到 `build/out/vendor-python/`，或显式给 `--vendor-python`。**`--skip-build` 并不等于「不需要 vendor-python」** |
| 报「未安装 Nuitka」，可你只想 `--skip-build` | `check_nuitka()` 读的是**当前解释器**的 `importlib.metadata` —— 复用 `.pyd` 也照查 | 用带 nuitka 的 3.11 构建 venv 跑（见 `docs/10` §3.1）。别为此去改管线，那会让「构建环境自证」失效 |
| `pip install -r requirements.txt` 报 `No matching distribution found for attrs==26.1.0` | `runtime_requirements.source` 用了 `freeze` —— 把构建机的偶然版本固化了（该版本源上不存在） | 改回 `"source": "pyproject"`（默认，不 pin）。见 `docs/10` §6 #12 |
| `smoke.py` 报「skillos 由编译产物加载」失败，但 `import` 明明是好的 | package 模式下 `__file__` / `__spec__.origin` 都是**虚拟源码路径**，没有 `.pyd` 后缀 | 判据改用 `__loader__ == nuitka_module_loader` + `__compiled__.extension_filename`。见 `docs/10` §6 #13 |
| `smoke.py` 报 `skillos import` 退 3 / 一堆 `import_conflict` | 冒烟**没隔离 home**，扫到了开发机真实的 `~/.claude/skills`、`~/.workbuddy/skills`（同名 slug 撞车） | 冒烟必须同时设 `SKILLOS_ROOT` 与 `SKILLOS_HOME`（`run_env()` 已做）。入口层变量名分裂已修（`resolve_home()` 统一，见 `docs/10` §6 #11） |
| `smoke.py` 报 `No Python at '<旧路径>/python.exe'` | 冒烟 venv 是**上一个布局**建的：`pyvenv.cfg` 把基座解释器路径写死 | 已自愈 —— `_venv_python_works()` 发现 venv 跑不起来就删掉重建。手工处置：删 `build/out/smoke/venv` 后重跑（或 `--rebuild-venv`） |
| `FATAL: cannot use '--mingw64' on Python version 3.13 or higher` | 3.13+ 只有 MSVC 一条路（上游无 3.13 的 MinGW 构建） | 装 VS 后 `--compiler msvc`；或换 3.11/3.12；或交给 CI。`build.py` 已会秒级预检拦住 |
| 构建**卡住不动**、无报错、无产物 | Nuitka 在等交互式确认（下载 winlibs MinGW64 / Dependency Walker / ccache）—— `queryUser()` 阻塞在 `raw_input()` 上（实测空转 18 分钟） | ① 带 `--assume-yes-for-downloads`（管线已写死）；② `build.py::run()` 还会喂一串 `yes` 到 stdin 兜底 —— **手工跑 Nuitka 时这两样都别漏**。详见 `docs/10` §4.4 |
| 某个新开关报 unknown | Nuitka 跨版本改名（如 4.2 用 `--mode=standalone` 取代 `--standalone`） | `build.py` 的 `verify_flags()` 会点名；照提示改 |
| 产物换机器后起不来（Linux） | 缺 `patchelf` 修正 rpath | `apt-get install patchelf`（CI 已含） |
| `smoke.py` 报 `/ 返回 HTML` 失败 | `web/dist` 没构建或 `SKILLOS_WEB_DIST` 不对 | 先 `cd web && npm ci && npm run build` |
| `smoke.py` 报 `runtime/web/ 无 sourcemap` 失败 | 前端产物带了 `.map` → 源码泄漏 | `web/vite.config.ts` 必须 `build.sourcemap=false` |
| `smoke.py` 报第 0 步 `SKILL.md 在包根` 失败 | 装配出的不是 skill 包（旧布局残留，或 `assemble.skill_src` 指错） | 确认 `nuitka.json` 的 `assemble.skill_src = "skill/skillos"`；必要时删掉整个 `build/dist/skillos-<平台>` 重装 |
| 报路径过长（Windows） | 目录层级深 | 用 `--no-archive`，或缩短仓库路径 / 开长路径支持 |

失败时先看 `build/out/module/module.report.xml`（standalone 则是 `build/out/<target>.report.xml`）
—— Nuitka 把模块解析详情都写在那里。

## 入口文件为什么是四个

**仅 standalone 路线使用**（module 路线把整个包交给 Nuitka，没有入口文件）。

| 文件 | 用途 |
|---|---|
| `skillos_main.py` | **默认发行入口**。按 `argv[0]` 把同一个二进制分派成 CLI / MCP / 后端 |
| `skillos_server.py` | 单角色入口 + 被 `skillos_main.py` 复用（内含程序化 uvicorn） |
| `skillos_cli.py` / `skillos_mcp.py` | 单角色入口，仅当 `shared_runtime.enabled=false` 时使用 |

为什么是一个二进制：三个角色依赖集重叠，各编一份会让体积 ×3。详见 `docs/10` §2。
