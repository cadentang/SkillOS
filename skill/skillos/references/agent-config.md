# Agent 接入配置（MCP）

> 本文件说明如何把**本包自带**的 `skillos` 引擎接入 Agent Runtime。
> 工具以 `skillos_` 前缀暴露给 Agent。配置字段以各 Agent 官方文档为准，下面给的是可直接使用的示例。
>
> **关键点：不需要 `pipx install skillos`。** 引擎就在本包里（`runtime/skillos/` 下是编译产物），
> 用本包自带的 CPython 拉起即可 —— 用户机器不需要预装任何 Python。

## 0. 前置

先确认系统已经跑起来（后端 + daemon）：

```bash
python scripts/detect.py     # 只读：看发行形态 / 运行时来源
python scripts/bootstrap.py  # 幂等：装第三方依赖（国内加 --mirror cn）
python scripts/start.py      # 起后端 + daemon + Web
python scripts/status.py     # 复验
```

首次使用还要初始化本地仓库（**用本包自带运行时，不要用系统 Python**）：

```bash
# Windows —— <包> 换成本 skill 包的绝对路径
set PYTHONPATH=<包>\runtime\skillos
<包>\vendor\win-x64\python\python.exe -P -c "from skillos.cli.main import app; app()" init

# macOS / Linux —— 解释器换成 vendor/mac-arm64/python/bin/python3（或对应平台）
```

确认 stdio server 能起来（Ctrl-C 退出）：

```bash
<包>/vendor/win-x64/python/python.exe -P -c "from skillos.mcp.server import main; main()"
```

> `-P` 不能省：它阻止把当前工作目录塞进 `sys.path`，避免误加载到磁盘上同名的 `skillos/` 源码目录。

## 1. Claude Code

在项目级 `.mcp.json`（或用户级 MCP 配置）中注册：

```json
{
  "mcpServers": {
    "skillos": {
      "command": "<包>/vendor/win-x64/python/python.exe",
      "args": ["-P", "-c", "from skillos.mcp.server import main; main()"],
      "env": { "PYTHONPATH": "<包>/runtime/skillos" }
    }
  }
}
```

> `<包>` 是**本 skill 包的绝对路径**（例如 `C:/Users/you/.claude/skills/skillos`）。
> macOS / Linux 把 `command` 换成 `<包>/vendor/<平台>/python/bin/python3`。

## 2. Codex

在 `~/.codex/config.toml` 中注册：

```toml
[mcp_servers.skillos]
command = "<包>/vendor/win-x64/python/python.exe"
args = ["-P", "-c", "from skillos.mcp.server import main; main()"]
env = { PYTHONPATH = "<包>/runtime/skillos" }
```

## 3. 验证

- 在 Agent 里说「列出我的 Skill」→ 应触发 `skillos_list_skills`
- 说「把 <某 Skill> 分发到 Claude Code」→ 应先返回 `dry_run` 预览（含将消失的路径），确认后才落盘

## 4. 故障排查

| 现象 | 原因 | 处置 |
|---|---|---|
| 工具不存在 | MCP 未注册 / Agent 未重启 | 检查上面的配置项，重启 Agent |
| 连接失败 / 立刻退出 | `PYTHONPATH` 没指到 `runtime/skillos`，或路径写错 | 用第 0 节那条 `-c` 命令手工验证能不能起来 |
| `ModuleNotFoundError: skillos` | `PYTHONPATH` 缺失，或用了系统 Python（ABI 不符） | 必须用**本包自带**的解释器，且 `PYTHONPATH=<包>/runtime/skillos` |
| 本地仓库未初始化（工具报仓库不存在） | 没跑过 `init` | 按第 0 节跑一次 `init`（幂等） |
| 引擎相关操作超时 | 依赖常驻 daemon / 后端 | 确认 `python scripts/status.py` 显示后端与 daemon 都在跑 |
| 分发被拦下并返回 `GATE_FAILED` | 质量门禁 `error` 级未通过 | 看规则明细，去 Web 控制台处理（**不可绕过**） |
| 分发被拦下并返回冲突 | 目标存在不属于本工具的改动 | 去 Web 控制台做整文件四选一 |

## 5. 安全提醒

- 写入类工具**默认 `dry_run=true`**，只预览不落盘；显式传 `dry_run=false` 才执行
- MCP 参数里**没有** `force` / `on_conflict` / 跳过门禁的开关 —— 不要尝试传入
- 会删内容的写入必须先列出将消失的路径等人确认；**不要在自动化脚本里替用户确认**
