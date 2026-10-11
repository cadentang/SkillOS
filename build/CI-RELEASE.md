# CI 编译发行手册（实操经验 · 可反复使用）

> 来源：2026-10-10 首次 CI 发行实战（run 38063781796 全绿）。
> 定位：`docs/10-发行与编译.md` 是设计文档；本文是**动手操作手册**——每次打新版都照这里走。

---

## 0. 全景：一条 tag 触发 4 平台

```
git tag v0.1.1 && git push origin v0.1.1
        │ 触发 .github/workflows/release.yml（矩阵）
        ▼
┌─────────────┬──────────────┬──────────────┬──────────────┐
│ win-x64     │ linux-x64    │ mac-arm64    │ mac-x64      │
│ windows-    │ ubuntu-      │ macos-15     │ macos-15-    │
│ latest      │ latest       │              │ intel        │
└─────────────┴──────────────┴──────────────┴──────────────┘
   产物 artifact：skillos-<平台>（zip，retention 14 天）
```

参考耗时（首跑）：web 62s / win 300s / linux 129s / mac-arm 215s / mac-x64 325s。
日常 CI（pytest/lint）不受影响，走 `ci.yml`，mac 不进日常矩阵。

---

## 1. 发新版的完整 checklist

1. **改代码 → 本地自证**（见 §3），确认工作区干净
2. **提交 + 推 main**：`git push origin main`（注意：SSH 推送必须在沙箱外跑，见 §5-坑1）
3. **打 tag 并推**：
   ```bash
   git tag v0.1.1
   git push origin v0.1.0   # 首次；tag 已存在且要覆盖时用 git tag -f + push -f
   ```
4. **盯 run**：https://github.com/cadentang/SkillOS/actions
5. **取产物**（见 §4）→ `build/smoke.py` 冒烟 → 分发

版本号约定：tag 形如 `v0.1.1`，workflow 里 `normalize_version()` 去掉 `v` 前缀写入产物元数据。
**tag 打错/CI 失败要重跑**：`git tag -f v0.1.1 <修复提交> && git push -f origin v0.1.1`（会触发新 run，旧 run 记录保留）。

---

## 2. CI 侧发生了什么（release.yml 逐步）

每个平台 job：
1. checkout → 装 Python 3.11（`actions/setup-python`）
2. `pip install "nuitka>=4.2,<5" uv`（**已锁版本**，防 5.0 破坏）+ `pip install -e .`
3. `PYTHONUTF8=1 python build/build.py --version-string $VERSION`（**已设 UTF-8**）
4. 上传 artifact `skillos-<平台>`；失败时上传 `nuitka-report-<平台>`（报告压缩比约 13:1，12KB zip ≈ 130KB XML）

> Nuitka **不支持交叉编译**：mac 产物只能在 mac runner 出。macOS 两架构是**两个不同标签**
> （`macos-15`=arm64、`macos-15-intel`=x64），没有「通用」标签；**别用 `macos-13`**（已退役，任务直接失败）。
> 私有仓库 mac runner 按 10× 计费分钟；公开仓库免费。

---

## 3. 本地构建（Windows，改 build 脚本/配置后的快速自证）

```bash
# 首次：建隔离构建 venv（在仓库根）
"D:/App/Python/Python311/python.exe" -m venv build/.venv-build
build/.venv-build/Scripts/python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "nuitka>=4.2,<5" uv
build/.venv-build/Scripts/python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e .

# 构建全流程（首次约 11 分钟，之后 ccache 命中约 5 分钟）
build/.venv-build/Scripts/python.exe build/build.py --version-string v0.1.1

# 冒烟（四重自证，别跳过）
build/.venv-build/Scripts/python.exe build/smoke.py

# 只想看会执行什么命令：--dry-run
```

关键机制（改配置前先懂）：
- **D1 路线 = 编译扩展模块 + 随包便携运行时**：`--mode=package` 只编 `skillos` 包本身；`exclude_site_packages` + `--nofollow-import-to` 把第三方挡在编译外，运行期从随包 venv 取
- `verify_flags()` 拿 `nuitka --help` 逐条对账开关名——加新开关后必须让这步跑过
- ABI 锁定 cp311：产物名 Windows 为 `skillos.cp311-win_amd64.pyd`，**mac/linux 为 `skillos.cpython-311-darwin.so` / `-x86_64-linux-gnu.so`**——同一 ABI 两种写法，校验两种都认
- `build/.venv-build/` 已 gitignore；`build/` 其余是源码，**根 .gitignore 不得写 `build/`**

---

## 4. 取 CI 产物（两种方式）

**方式 A · 浏览器（最省事，无需 token）**
打开 run 页 → 底部 Artifacts 区 → 逐个下载 zip。
`https://github.com/cadentang/SkillOS/actions/runs/<run_id>`

**方式 B · API + fine-grained token（可脚本化）**

> 注意：**Actions artifact 下载必须带 token，公开仓库也不例外**（匿名 401）。

```bash
TOKEN="<fine-grained token，需 Actions: Read>"
RUN=38063781796
# 1) 列产物拿 id
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://api.github.com/repos/cadentang/SkillOS/actions/runs/$RUN/artifacts"
# 2) 按 id 下载（收到的是 302 → CDN zip，curl -L 跟随）
curl -L -H "Authorization: Bearer $TOKEN" \
  -o skillos-mac-arm64.zip \
  "https://api.github.com/repos/cadentang/SkillOS/actions/artifacts/<id>/zip"
```

token 只在命令里临时用，**不写入任何项目文件、不落盘**。

**产物落位**：解压到 `build/dist/`（gitignored），得到 `build/dist/skillos-<平台>/`，
根目录即 skill 包（`SKILL.md` 在根）。Windows 机器装 win-x64 那份即可。

> ⚠️ **两个解压陷阱（实战踩过）**：
> ① artifact zip 内部是**两层结构**：`skillos-0.1.0-<平台>.zip`（交付 zip）+ `skillos-<平台>/`（解包目录）——
>    解压后把内层目录上提一层再落位；
> ② 若 `build/dist/skillos-<平台>/` 已有**本地旧构建**，解压会直接**合并污染**（不会报错！）——
>    先 `mv` 挪走旧目录再解压（别用 `rm -rf` 大目录）。
>
> 解压后用 `BUILD-INFO.json` 验来源：`git_commit` 应等于本次 tag 指向的提交、`nuitka` 符合锁定版本、
> `source_leak_check.clean == true`、`executables` 的文件名符合平台命名（win `cp311-win_amd64.pyd` /
> mac `cpython-311-darwin.so` / linux `cpython-311-x86_64-linux-gnu.so`）。

---

## 5. 踩坑清单（血泪，勿重蹈）

| # | 坑 | 症状 | 解法（已固化） |
|---|---|---|---|
| 1 | **本机沙箱读不了 `~/.ssh/known_hosts`/私钥** | `git push` 报 `Host key verification failed` | git push/pull 一律在**沙箱外**跑（`dangerouslyDisableSandbox` 或用户终端） |
| 2 | **GitHub Windows runner stdout 是 cp1252** | python 脚本打印中文秒崩（`'charmap' codec can't encode`），step 耗时 0s | build.py / smoke.py 已加 `sys.stdout/stderr.reconfigure(encoding="utf-8")`；workflow 已设 `PYTHONUTF8=1`。**新写 CI 脚本凡是 print 中文必须带这个兜底** |
| 3 | **Nuitka 产物名随平台变** | mac/linux 被自家 ABI 校验误杀（`没有 cp311`），其实编成功了 | 校验同时认 `cp311` 与 `cpython-311`（无小数点） |
| 4 | 依赖不封顶 | CI 装到新版 Nuitka/ruff 后**没改代码也变红** | `nuitka>=4.2,<5`、`ruff>=0.16.9,<0.17`、`mcp>=1.2,<2`；新增依赖同理给上限 |
| 5 | `git add build` 吞 venv | 65MB/3556 文件的 `.venv-build` 险些入库 | `.gitignore` 已含 `build/.venv-build/`；`git check-ignore` 验证后再 add |
| 6 | 匿名访问 Actions | 日志/artifact API 全 401/403 | 见 §4 方式 B，fine-grained token 最小权限（Actions: Read） |
| 7 | pip 直连官方源超时 | 9m46s 静默/超慢 | 一律 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| 8 | 历史改写残留 | filter-branch 后 `refs/original/*`、reflog 还指向旧提交 | `update-ref -d` 逐个删 + `reflog expire --expire=now --all` + `gc --prune=now`；改写前先 `git bundle` 备份 |
| 9 | 公开仓库也要 noreply 邮箱 | 真实邮箱随历史暴露 | `<user_id>+<login>@users.noreply.github.com`；历史改写用 `--env-filter` |
| 10 | 推大包被掐 | push 卡死 | `http.postBuffer=524288000` + `http.version=HTTP/1.1` + 后台重试；SSH 比 HTTPS 稳（本机 HTTPS 到 github.com 被代理拦） |

---

## 6. 排查 CI 失败的固定套路

1. `https://api.github.com/repos/<org>/<repo>/actions/runs/<id>/jobs` —— 匿名可读，看**每个 step 的耗时**（0 秒 = 没进正题；几十秒 = 真跑了）
2. 失败时 job 页有 annotations（匿名可读），含日志行号
3. 拿不到完整日志就让有浏览器权限的人贴 step 原文（最准）
4. 本地复现：纯净克隆 `git clone <本地仓库> /tmp/sim` + 复用 `build/out/module` 已编译载荷跑 `--skip-build --no-archive`，1-2 分钟分离「编译问题 vs 装配问题」
5. 别凭「有 report artifact」断定成功——Nuitka 失败时也写报告
