# release.md —— 制作 SKILLOS 发行包 · 操作手册

> 触发词：**创建SKILLOS / 更新SKILLOS**。本文是 agent 执行视角；打包侧设计文档在仓库
> `docs/10-发行与编译.md`，10 条踩坑全记录在仓库 `build/CI-RELEASE.md`。

## 0. 全景

```
git tag vX.Y.Z && git push origin vX.Y.Z      ← 沙箱外跑
        │ 触发 release.yml（四平台矩阵 + web）
        ▼  约 3-6 分钟
Artifacts：skillos-win-x64 / skillos-linux-x64 / skillos-mac-arm64 / skillos-mac-x64 / web-dist
        │ release_pull.py（token 一次性传入）
        ▼  先清旧、只留最新
skill/skillos/vendor/dist/                    ← 最终归档位（gitignored）
```

参考耗时：web 62s / win 300s / linux 129s / mac-arm 215s / mac-x64 325s。

## 1. 创建 SKILLOS（新版本全流程）

### 1.1 前置自证

```bash
cd <SkillOS 仓库根>
git status --porcelain          # 必须干净
ruff check skillos tests        # CI 同款 lint
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

### 1.5 拉产物归位

先让用户现场生成 fine-grained token：GitHub → Settings → Developer settings →
**Fine-grained tokens** → Generate new token：仅勾 `SkillOS` 仓库、**Actions: Read-only**、7 天过期。

```bash
SKILLOS_GH_TOKEN=<token> python scripts/release_pull.py          # 最新成功 run
SKILLOS_GH_TOKEN=<token> python scripts/release_pull.py --run <run_id>
SKILLOS_GH_TOKEN=<token> python scripts/release_pull.py --list   # dry-run 只看清单
```

脚本行为：下载 5 个 artifact → 从平台 artifact 的**两层结构**中只取内层交付 zip →
清空 `skill/skillos/vendor/dist/` 旧内容 → 落新 → 写 `RELEASE-INFO.json`。

### 1.6 验证（三条全过才算完成）

1. `vendor/dist/RELEASE-INFO.json`：`version` == tag 去 v、`head_sha` == tag 指向提交、5 个文件 sha256 在案
2. 抽查任一交付 zip 内 `BUILD-INFO.json`：`git_commit` 一致、`nuitka` 符合锁定版本
   （`>=4.2,<5`）、`source_leak_check.clean == true`
3. ABI 命名符合平台：win `skillos.cp311-win_amd64.pyd` / mac `skillos.cpython-311-darwin.so` /
   linux `skillos.cpython-311-x86_64-linux-gnu.so`

### 1.7 冒烟 + 汇报

本机 Windows 时用仓库 `build/smoke.py --dist <win 解包目录>`（42 项断言：CLI / MCP 八工具白名单 / 后端+SPA）。

汇报至少含：版本、run 链接、各平台产物大小、vendor/dist 最终清单、冒烟结论，并**提醒吊销 token**。

## 2. 更新 SKILLOS（只刷新归档）

```bash
SKILLOS_GH_TOKEN=<token> python scripts/release_pull.py
```

自动取最新成功 run；`vendor/dist/` 只留最新。若用户指名某次 run，加 `--run <id>`。

## 3. token 安全约定（不可违）

- 只经 `SKILLOS_GH_TOKEN` 环境变量或 `--token-stdin` 管道进入进程
- **不写入任何文件 / 项目配置 / 命令历史 / 日志**；不在回复中回显
- 用最小权限：仅 SkillOS 仓库 + Actions Read-only + 7 天过期；用完建议吊销
- 仓库内任何文件出现 `github_pat_` 字样都是事故，立即删除并吊销

## 4. 踩坑速查

| 坑 | 解法（已固化处） |
|---|---|
| Windows runner stdout cp1252，中文日志秒崩 | build.py/smoke.py 已加 UTF-8 reconfigure；workflow 已设 PYTHONUTF8=1 |
| Nuitka 产物名随平台变（cp311 vs cpython-311） | build.py ABI 校验两种写法都认 |
| artifact zip 两层结构 | 脚本只取内层交付 zip |
| 归档区旧文件导致解压静默合并 | 脚本先清后写；手动操作先 mv 挪走旧的（勿 rm -rf 大目录） |
| 依赖不封顶 → CI 没改代码也红 | nuitka `<5` / ruff `<0.17` / mcp `<2` 已锁；新依赖必须给上限 |
| pip 直连超时 | 一律清华源 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| macOS runner 标签 | `macos-15`(arm64) / `macos-15-intel`(x64)；`macos-13` 已退役别用 |
| 匿名访问 Actions | artifact 下载必须 token；job 列表与 annotations API 匿名可读 |
