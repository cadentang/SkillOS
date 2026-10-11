---
name: skillos-builder
description: 制作 SKILLOS 发行包的打包流水线。当用户说「创建SKILLOS」「更新SKILLOS」时使用：本地构建 → GitHub Actions CI 四平台编译 → 拉取四平台产物与 web 前端 → 归档到 skill/skillos/vendor/dist/（只保留最新版本）→ 校验与冒烟。触发词：创建SKILLOS、更新SKILLOS、打包SKILLOS、SKILLOS发新版、构建SKILLOS。token 只经环境变量/stdin 一次性传入，不落盘、不进日志。
version: 0.1.0
agent_created: true
---

# skillos-builder —— 制作 SKILLOS 发行包

**本 skill 只做一件事：把 SkillOS 变成可分发的发行包。** 它不描述 SkillOS 怎么用
（那是 `skill/skillos/` 的事），也不承载产品功能 —— 它是「工厂」，不是「产品」。

## 两个触发词

| 用户说 | 含义 | 走哪条流程 |
|---|---|---|
| **创建SKILLOS** | 首版，或代码有改动要出**新版本号** | 流程 A（7 步全流程） |
| **更新SKILLOS** | 代码没大改，只要**最新产物**（或重跑某版本） | 流程 B 拉产物 → **第 6 步交付前审查必跑** |

**先问一句再动手**：如果用户没说版本号，确认「沿用 v0.1.x 还是发新版本号？」——
新版本号要走流程 A 全程（打新 tag 触发 CI）；沿用旧版本直接走流程 B 拉最新成功 run。

## 关键事实（动手前知道，省一小时排错）

- 仓库：`github.com/cadentang/SkillOS`（公开）；**本机推/拉必须走 SSH**（HTTPS 被代理拦），
  且 **git push 必须在沙箱外跑**（沙箱读不了 `~/.ssh/known_hosts`，报 `Host key verification failed`）
- CI：`.github/workflows/release.yml`，**打 tag `vX.Y.Z` 才触发**，四平台矩阵
  （win-x64 / linux-x64 / mac-arm64 / mac-x64）+ web，全程约 3-6 分钟
- 产物：run 页 Artifacts 区，4 个 `skillos-<平台>` + `web-dist`，**14 天后过期**
- **Actions artifact 下载必须带 token（公开仓库也 401）** → 每次执行让用户现场生成
  fine-grained token（仅 SkillOS 仓库、Actions: Read-only、7 天过期），走环境变量或 stdin 传入
- 完整踩坑清单（10 条）在 SkillOS 仓库 `build/CI-RELEASE.md`；本包 `references/release.md` 是操作视角的手册

## 流程 A · 创建（新版本全流程）

**第 1 步 · 前置自证**（在 SkillOS 仓库根）

```bash
git status --porcelain        # 必须干净；有改动先提交
ruff check skillos tests      # CI 同款 lint，红了先修
```

**第 2 步 · 提交推送**（push 在沙箱外）

```bash
git push origin main
```

**第 3 步 · 打 tag 触发 CI**

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
# tag 打错要重打：git tag -f vX.Y.Z <提交> && git push -f origin vX.Y.Z
```

**第 4 步 · 等 CI 全绿再继续**
盯 `https://github.com/cadentang/SkillOS/actions`。判断技巧：job 列表 API 匿名可读，
**某 step 耗时 0 秒 = 没进正题就死了**；四平台 `Compile` 全 success 才算绿。

**第 5 步 · 拉产物归位**（本包脚本，自动清旧、只留最新）

```bash
SKILLOS_GH_TOKEN=<用户刚生成的 token> python scripts/release_pull.py            # 最新成功 run
SKILLOS_GH_TOKEN=xxx python scripts/release_pull.py --run <run_id>             # 指定 run
SKILLOS_GH_TOKEN=xxx python scripts/release_pull.py --list                     # 先看清单不下载
```

**第 6 步 · 交付前审查（必跑，不过不交付）**

skillos 最终要交给客户，所以产物必须过审——审查两件事：**产品包流程自洽**（SKILL.md 描述 ↔
脚本实现 ↔ manifest ↔ 实物产物）与**与项目约定不冲突**（端口 / ABI / 路径 / 依赖上限 / 红线）。

```bash
python scripts/audit_skillos.py                                  # 源码包 + vendor/dist 归档（默认）
python scripts/audit_skillos.py --archive <交付 zip>              # 直接审交付 zip 内部
python scripts/audit_skillos.py --package <解包目录> --skip-release  # 审发行包实物形态
```

覆盖面（26 项）：结构完整性 / SKILL.md 引用的脚本与路径是否真实存在 / 文档承诺的命令行参数
是否真被实现 / 孤儿脚本 / 形态判定（源码 vs frozen）与 ABI 命名 / 零安装件（start.* + 便携
CPython + runtime/web）/ runtime 下无 .py 泄漏 / 凭据扫描 / 与 docs 的端口·state_dir·ABI 锚点
对比 / 依赖封顶 / 归档 sha256 + 与 BUILD-INFO 的 commit·nuitka 版本交叉核对。

**脚本覆盖不了的语义项**（描述是否覆盖全部触发场景、客户话术、红线一致性）照
`references/audit-checklist.md` 人工过一遍。

> 审查出现 **fail 一律不得交付**；修复要走流程 A 出新版本，禁止「先交付后修」。

**第 7 步 · 冒烟 + 汇报**
- 本机是 Windows：用仓库 `build/smoke.py --dist <解包后的 win 产物>` 跑四重自证（42 项断言）
- 汇报：版本 / run 链接 / 各平台产物大小 / vendor/dist 落位 / **审查结论（含 warn 与决策项）** / 冒烟结果
- **提醒用户吊销刚才的 token**（它在对话里出现过）

## 流程 B · 更新（只刷新产物归档）

```bash
SKILLOS_GH_TOKEN=xxx python scripts/release_pull.py
```

拉「最新一次成功的 release run」，`vendor/dist/` 旧内容自动清掉，只留最新。

## 落位规约（产物去哪）

```
skill/skillos/vendor/
├── <平台>/python/ …            # 构建期便携运行时（build.py 注入，与本 skill 无关，别动）
└── dist/                       # ← 本 skill 的归档区（gitignored，只保留最新版本）
    ├── skillos-<版本>-win-x64.zip      # 交付 zip（从 artifact 两层结构中取内层）
    ├── skillos-<版本>-mac-arm64.zip
    ├── skillos-<版本>-mac-x64.zip
    ├── skillos-<版本>-linux-x64.zip
    ├── web-dist.zip                    # 前端原始包
    └── RELEASE-INFO.json               # 版本/run/commit/sha256 台账
```

## 五条铁律

1. **token 不落盘**：只走环境变量/stdin；不写进任何文件、命令历史、日志；用完提醒吊销。
2. **CI 没全绿不拉产物**；拉完不验 BUILD-INFO 不算完成。
3. **vendor/dist 只留最新**：`release_pull.py` 拉取前自动清旧——不许手动攒多版本。
4. **不污染 `skill/skillos/` 产品定义**：本 skill 的一切改动只进 `skillos-builder/` 和
   `skill/skillos/vendor/dist/`（gitignored）；不改对方的 SKILL.md / scripts / manifest。
5. **审查不过不交付**：`audit_skillos.py` 出 fail 必须修掉、走流程 A 出新版本；
   warn 与决策项（如内置演示凭据）要在汇报里向用户点名，不静默放过。

## 踩坑速查（详情见仓库 build/CI-RELEASE.md）

| 坑 | 一句话解法 |
|---|---|
| Windows runner stdout 是 cp1252，脚本打中文秒崩 | CI 脚本必须有 `sys.stdout.reconfigure(encoding="utf-8")`（已固化） |
| Nuitka 产物名随平台变（`cp311-*.pyd` vs `cpython-311-*.so`） | 校验两种写法都认（已固化在 build.py） |
| artifact zip 是两层（交付 zip + 解包目录） | 只取内层交付 zip（脚本已处理） |
| 本地 `vendor/dist` 有旧文件时解压会静默合并 | 脚本先清后写；手动操作先 `mv` 挪走旧的 |
| 依赖不封顶 → CI 没改代码也红 | nuitka `<5`、ruff `<0.17`、mcp `<2` 已锁；新增依赖同理 |
| pip 直连官方源超时 | 一律 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |

## 引用资源

- `references/release.md` —— 操作视角的完整手册（含 API 命令原文）
- `references/audit-checklist.md` —— 交付前人工复核清单（脚本查不了的语义项）
- `scripts/release_pull.py` —— 拉产物归位（纯标准库；`--list` dry-run）
- `scripts/audit_skillos.py` —— 交付前审查（只读；源码包 / 发行包 / 交付 zip 三种入口）
- 仓库 `build/CI-RELEASE.md` —— 打包侧 10 条踩坑全记录
