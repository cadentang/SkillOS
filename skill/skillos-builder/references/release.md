# release.md —— 制作 SKILLOS 发行包 · 操作手册

> 触发词：**创建SKILLOS / 更新SKILLOS**。本文是 agent 执行视角；打包侧设计文档在仓库
> `docs/10-发行与编译.md`，10 条踩坑全记录在仓库 `build/CI-RELEASE.md`。

## 0. 全景

```
① 版本一致性核对（本地 HEAD / origin/main / 待发 tag / 已有归档）  ← 不齐不开工
        │ 不一致 → AskUserQuestion 让用户选：拉取 / 覆盖 / 换号 / 中止
        ▼
git tag vX.Y.Z && git push origin vX.Y.Z      ← 沙箱外跑
        │ 触发 release.yml（四平台矩阵 + web）
        ▼  约 3-6 分钟
Artifacts：skillos-win-x64 / skillos-linux-x64 / skillos-mac-arm64 / skillos-mac-x64 / web-dist
        │ publish job：同时挂到该 tag 的 GitHub Release（匿名可下）
        │ release_pull.py（默认走 Release，**免 token**）
        ▼  先清旧、只留最新
skill/skillos/vendor/dist/                    ← 最终归档位（gitignored）
```

参考耗时：web 62s / win 300s / linux 129s / mac-arm 215s / mac-x64 325s。

## 1. 创建 SKILLOS（新版本全流程）

### 1.0 版本一致性核对（必做，不一致必须让用户拍板）

**首选一条命令**（只读，不会替你 pull / rebase / push -f / 换号；block 即退出码 1）：

```bash
python scripts/check_sync.py --tag vX.Y.Z   # 默认先 fetch
python scripts/check_sync.py --no-fetch     # 沙箱内/离线（refs 可能过期）
python scripts/check_sync.py --json         # 机器可读
```

有 `SKILLOS_GH_TOKEN` 时会顺带查远程最新成功 run 与归档比对；`--tag` 省略则跳过版本号维度。

**手动核对**（兜底口径，SSH 命令沙箱外跑）：

```bash
cd <SkillOS 仓库根>
git fetch origin --tags --prune        # 只读
git status --porcelain                 # 空 = 工作区干净
git rev-parse --short HEAD             # 本地 HEAD
git rev-parse --short origin/main      # 远程主干
git log --oneline origin/main..HEAD    # 本地领先（= 本次要发的改动）
git log --oneline HEAD..origin/main    # 本地落后（= 远程有新提交）
git tag --list 'v*' --sort=-v:refname | head -5
git ls-remote --tags origin            # 远程真实 tag，别信本地缓存
```

| 情形 | 判据 | 处理 |
|---|---|---|
| 完全对齐 | 工作区空 + 领先/落后两区间都空 | 继续 1.1 |
| 本地领先 | `origin/main..HEAD` 非空、`HEAD..origin/main` 空 | 正常，1.2 推送即可；向用户报「领先 N 个提交」 |
| 本地落后 | `HEAD..origin/main` 非空 | **停**：基于旧代码发版 = 产物落后 |
| 分叉 | 两区间都非空 | **停**：先决定 merge / rebase / 放弃本地 |
| 工作区脏 | `git status --porcelain` 非空 | **停**：确认哪些提交、哪些丢弃 |

版本号另查两件事：**待发 tag 远程已存在**（`git ls-remote --tags origin vX.Y.Z` 有输出）→ 覆盖还是换号？
**待发版本号低于远程最高 tag** → 这是回退，必须用户确认意图。

归档另查一件事：`vendor/dist/RELEASE-INFO.json` 的 `head_sha` 若**不在远程历史里**（来自未推送的
本地提交）→ 历史产物与远程代码不符，**停下报告**；仅落后于远程最新 run 则重拉即可，不阻塞。

**不一致时的四个标准选项**（AskUserQuestion，不得自行 pull / rebase / push -f / 换号）：
① 拉取远程后重来（`git pull --ff-only`） ② 推送本地覆盖远程 ③ 换新版本号重发 ④ 中止，用户自己处理

### 1.1 前置自证

```bash
ruff check skillos tests        # CI 同款 lint
git status --porcelain          # 提交前复确认干净
```

### 1.2 提交推送（push 在沙箱外）

```bash
git push origin main            # HTTPS 到 github.com 被本机代理拦，必须 SSH remote
```

### 1.3 打 tag 触发 CI

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
# 重打：git tag -f vX.Y.Z <提交> && git push -f origin vX.Y.Z（会触发新 run，旧 run 保留）
```

### 1.4 等 CI 全绿

- 页面：`https://github.com/cadentang/SkillOS/actions`
- job 列表 API（**匿名可读**，看每个 step 耗时——0 秒 = 没进正题就死了）：

```bash
curl -s "https://api.github.com/repos/cadentang/SkillOS/actions/runs/<run_id>/jobs?per_page=20"
```

- 四个 `Compile (<平台>)` job 全 success 才算绿；失败时 job 页 annotations 也有线索。

### 1.5 拉产物归位（默认免 token）

```bash
python scripts/release_pull.py                     # 最新 Release（无需 token）
python scripts/release_pull.py --tag v0.2.2        # 指定版本
python scripts/release_pull.py --list              # dry-run 只看清单
python scripts/release_pull.py --jobs 8            # 并发连接数（默认 5）
```

脚本行为：**并发**下载 5 个文件（默认 5 连接）→ 清空 `skill/skillos/vendor/dist/` 旧内容 →
落新 → 写 `RELEASE-INFO.json`（含 `source: release|artifact`）。

**两条通道**（`--source auto` 默认：先 Release，不行才回退 artifact）：

| 通道 | 需要 token | 取法 |
|---|---|---|
| **release**（正常） | **不需要** | CI `publish` job 已把 5 个交付 zip 挂到该 tag 的 Release，`browser_download_url` 直接下 |
| artifact（回退） | 需要 | `--source artifact` 或 `--run <id>`；artifact 下载强制鉴权（公开仓库也 401） |

回退时现场生成 fine-grained token：GitHub → Settings → Developer settings →
**Fine-grained tokens** → 仅勾 `SkillOS` 仓库、**Actions: Read-only**、7 天过期；用完吊销。

**为什么必须并发**：下载是 302 → Azure 签名 CDN，**单连接会被限速**。实测同一批
390MB：单线程 51 KB/s（≈2 小时），5 并发聚合 700 KB/s（≈10 分钟）。慢的时候 `--jobs 8`。

下载全程落系统临时目录（`%TEMP%/skillos-release-*`），**全部成功才动归档区** ——
中途失败不会留下半套归档，也不会污染 `vendor/dist/`。

### 1.6 验证（三条全过才算完成）

1. `vendor/dist/RELEASE-INFO.json`：`version` == tag 去 v、`head_sha` == tag 指向提交、5 个文件 sha256 在案
2. 抽查任一交付 zip 内 `BUILD-INFO.json`：`git_commit` 一致、`nuitka` 符合锁定版本
   （`>=4.2,<5`）、`source_leak_check.clean == true`
3. ABI 命名符合平台：win `skillos.cp311-win_amd64.pyd` / mac `skillos.cpython-311-darwin.so` /
   linux `skillos.cpython-311-x86_64-linux-gnu.so`

### 1.7 冒烟 + 汇报

本机 Windows 时用仓库 `build/smoke.py --dist <win 解包目录>`（42 项断言：CLI / MCP 八工具白名单 / 后端+SPA）。

汇报至少含：版本、Release 链接（或 run 链接）、各平台产物大小、vendor/dist 最终清单、冒烟结论。
只在回退了 artifact 通道并用了 token 时，提醒吊销。

## 2. 更新 SKILLOS（只刷新归档）

**先做 §1.0 的「归档维度」核对**（`RELEASE-INFO.json` 的 `head_sha` / `run_id` vs 远程最新成功 run）：
归档来自未推送的本地提交 → 停下报告；只是落后 → 直接重拉。

```bash
python scripts/release_pull.py        # 免 token
```

自动取最新 Release；`vendor/dist/` 只留最新。若指名某个版本，加 `--tag vX.Y.Z`。
拉完仍要过 §1.6 验证 + 交付前审查（`audit_skillos.py`）。

## 3. token 安全约定（回退 artifact 通道时适用）

- 只经 `SKILLOS_GH_TOKEN` 环境变量或 `--token-stdin` 管道进入进程
- **不写入任何文件 / 项目配置 / 命令历史 / 日志**；不在回复中回显
- 用最小权限：仅 SkillOS 仓库 + Actions Read-only + 7 天过期；用完建议吊销
- 仓库内任何文件出现 `github_pat_` 字样都是事故，立即删除并吊销
- **没有 token 不等于可以停下**：先确认该 tag 有没有 Release 附件（正常发版都有）

## 4. 踩坑速查

| 坑 | 解法（已固化处） |
|---|---|
| Windows runner stdout cp1252，中文日志秒崩 | build.py/smoke.py 已加 UTF-8 reconfigure；workflow 已设 PYTHONUTF8=1 |
| Nuitka 产物名随平台变（cp311 vs cpython-311） | build.py ABI 校验两种写法都认 |
| artifact zip 两层结构 | Release 通道直取交付 zip；artifact 通道脚本只取内层 |
| 归档区旧文件导致解压静默合并 | 脚本先清后写；手动操作先 mv 挪走旧的（勿 rm -rf 大目录） |
| 依赖不封顶 → CI 没改代码也红 | nuitka `<5` / ruff `<0.17` / mcp `<2` 已锁；新依赖必须给上限 |
| pip 直连超时 | 一律清华源 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| macOS runner 标签 | `macos-15`(arm64) / `macos-15-intel`(x64)；`macos-13` 已退役别用 |
| 匿名访问 Actions | artifact 下载必须 token；job 列表与 annotations API 匿名可读 |
| **拉产物卡在「要一个 token」** | Release assets 匿名可下 → 正常发版零凭据；只有早期 run 才回退 artifact |
| 单连接下载被 CDN 限速（390MB 要 2 小时） | `release_pull.py` 默认 5 并发（`--jobs 8` 更快）；进度看 `%TEMP%/skillos-release-*/`，**归档区在全部下完前不会变** |
| 远程有本地不知道的提交，照旧基于旧代码发版 | §1.0 `git fetch` + 区间比对；落后/分叉一律停下问用户 |
| 待发 tag 已被占用 / 低于远程最高 tag（回退） | §1.0 `git ls-remote --tags origin`；停下问「覆盖还是换号」 |
