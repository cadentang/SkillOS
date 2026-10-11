# audit-checklist.md —— 交付前人工复核清单

> `scripts/audit_skillos.py` 覆盖机器可查的 26 项；本清单是**脚本查不了的语义项**。
> 每次「创建SKILLOS / 更新SKILLOS」都要逐条过一遍，结论写进汇报。
> 背景：**skillos 最终交付给客户** —— 出错成本不在我们自己身上。

## A. 描述准确性（SKILL.md ↔ 真实行为）

| # | 看什么 | 判据 |
|---|---|---|
| A1 | 「何时使用」是否覆盖全部入口 | 三类场景（启动/停止/自检、Skill 资产查询管理、创建更新）都有对应触发语；漏一类 = 客户说了话却没人动 |
| A2 | 形态判据描述与实现一致 | SKILL.md 说「判据是能不能真的找到编译产物」→ 对应 `scripts/_common.is_frozen_mode()` / `detect.py` 的实现（脚本已查参数，这一句要人读） |
| A3 | 「一条铁律」的三条硬标准仍与实现一致 | 健康检查 JSON 形状、`pid_alive`、`~/.skillos/token` 三者与 `status.py` 实际输出对得上 |
| A4 | 命令示例可直接复制执行 | 抽查 2-3 条命令按字面敲下去能跑（尤其带 `--json`、`--mirror cn` 的） |
| A5 | 端口 / 目录等数字与 manifest 一致 | SKILL.md 里出现的 9080 / 5173 / `~/.skillos` 与 `manifest.json` 一致 |

## B. 与项目本身冲突（跨文档一致性）

| # | 看什么 | 判据 |
|---|---|---|
| B1 | 端口约定 | `docs/09-部署手册.md` 统一 9080；manifest / compose / Dockerfile / 前端代理须一致 |
| B2 | ABI 锁定 | `docs/10-发行与编译.md` §2：产物只认 CPython 3.11；发行包 `manifest.frozen.python_pin` 必须写死 3.11 |
| B3 | 状态目录隔离 | `FR-2.4.3` / docs/09 §隔离：一切状态在 `~/.skillos`，**绝不落项目目录** |
| B4 | 红线 | docs/00 红线清单：不代装系统运行时 / 不守护进程 / token 只落 `~/.skillos` / Agent 不执行危险决策 / 门禁不可绕 —— SKILL.md 的「边界」章节必须仍逐条对应 |
| B5 | MCP 白名单 | `OUT-14` 八工具；SKILL.md 的工具清单表与 `skillos/mcp/server.py` 的工具集必须一致（多一个少一个都是冲突） |
| B6 | 依赖上限 | pyproject 中 nuitka `<5` / ruff `<0.17` / mcp `<2`；新增依赖必须给上限（脚本已查三项，新加的靠人） |

## C. 客户交付面

| # | 看什么 | 判据 |
|---|---|---|
| C1 | 包内不得出现内部信息 | 无仓库内文档编号（`docs/10 §2` 这类）、无内网地址、无开发机路径、无未公开路线规划 |
| C2 | 演示凭据的处置话术 | `manifest.account` 内置演示账号是**已知保留决策**；交付时必须写明「演示凭据，部署后立即改密」，不能让它默默存在 |
| C3 | README / SKILL.md 面向谁 | `README.txt` 面向终端用户（双击 start.cmd 即可），不写开发细节；`SKILL.md` 面向 Agent，可写机制 |
| C4 | 授权与来源声明 | 若含第三方便携运行时（uv / MinGit / CPython），`vendor/README.md` 的许可与来源说明仍在包内（本次审查发现的缺口：交付包缺该文件） |
| C5 | 大小与网络预期 | 各平台包体积（win ≈32MB / mac ≈47MB / linux ≈63MB）与 README 描述一致；离线用法有说明 |

## D. 汇报模板（每次创建/更新后给用户）

```
版本：vX.Y.Z           run：<链接>            commit：<sha>
产物：win-x64 <MB> / mac-arm64 <MB> / mac-x64 <MB> / linux-x64 <MB> / web-dist <MB>
落位：skill/skillos/vendor/dist/（仅最新，台账 RELEASE-INFO.json）
审查：audit_skillos.py → fail N / warn N；人工清单 A1-C5 的结论
冒烟：build/smoke.py → 42 项断言 <结论>
决策项：<演示凭据 / 已知 warn 的处置>
提醒：吊销本次使用的 token
```
