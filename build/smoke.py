"""编译产物冒烟：对 **D1 发行目录** 做四重自证。

为什么必须有这个
----------------
「构建命令退出码为 0」不等于「产物能用」：Nuitka 最典型的失败是**编译成功、
运行期才炸**（某个库按字符串 import 了没被跟随的模块，或 Path(__file__) 找不到
没随包的数据文件）。那些恰好都是 `build/` 跑通、`import` 就崩的类型。
所以这个脚本不信任构建日志，只信任产物自己的行为。

D1 形态与 standalone 的关键区别
-------------------------------
发行物不再自足：`skillos` 是编译出来的扩展模块（`.pyd`/`.so`），第三方依赖由
随包 venv 提供。所以冒烟必须**先把发行物当成用户那样装一遍**（用自带的便携
CPython 建 venv → 装 requirements.txt），再以
`PYTHONPATH=<dist>/runtime/skillos` 跑起来。

产物形态（build.py 的装配结果）
------------------------------
`<dist>` **本身就是一个完整 skill 包**：

    <dist>/SKILL.md  manifest.json  scripts/  references/   ← skill 的根
    <dist>/runtime/skillos/   ← 编译产物（.pyd + 数据树）与 PYTHONPATH 注入点
    <dist>/runtime/web/       ← 前端 SPA
    <dist>/runtime/requirements.txt
    <dist>/vendor/<平台>/python/   ← 便携 CPython
    <dist>/start.cmd · start.sh    ← 零安装入口

四重自证
--------
  0. **形态与保密** 包根有 SKILL.md 与 scripts/；manifest.frozen 块存在；
                 运行时目录下**没有任何 .py 源码**；`web/**` 下**没有任何
                 sourcemap**；便携运行时与 requirements 在位
  1. **运行时**   用包内自带的 CPython 建 venv、装 requirements，并确认
                 `import skillos` 命中的是编译产物而不是某个源码目录
  2. **CLI**      `skillos --help` 退出码 0（Typer 能装配、子命令齐全）
  3. **MCP**      走完整 JSON-RPC：initialize → tools/list（八工具白名单）→
                 tools/call（dry_run 语义 / NOT_IMPLEMENTED 红线）
  4. **Server**   起 ASGI → /api/health 通 → `/` 返回 SPA → 深路由回退 → /api 不被吃掉

用法
----
    python build/smoke.py                          # 自动找 build/dist/skillos-<当前平台>
    python build/smoke.py --dist build/dist/skillos-win-x64
    python build/smoke.py --mirror cn              # 国内镜像装依赖（默认）
    python build/smoke.py --rebuild-venv           # 强制重建冒烟 venv
    python build/smoke.py --port 9288 --keep       # 保留临时 home 便于排查
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# 同 build.py：GitHub Actions 的 Windows runner 上 stdout 是管道、编码取到 cp1252，
# 而本脚本的检查标签含中文 —— 不在最早的这一步钉成 UTF-8，修好 build.py 之后
# 会在冒烟步再崩一次（同一根因，两个脚本）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):   # 非 TextIOWrapper / 已 detach
        pass

REPO = Path(__file__).resolve().parent.parent
DIST_ROOT = REPO / "build" / "dist"
SMOKE = REPO / "build" / "out" / "smoke"

_PY_GLOBS = ("python.exe", "*/python.exe", "bin/python3", "*/bin/python3", "bin/python")

# 与 <包根>/scripts/bootstrap.py 的预设保持一致（同一个项目、同一套口语习惯）。
# 这里不 import 那边，是因为 build/ 必须在「还没装依赖」的裸机上也能跑 —— 保持纯标准库。
_MIRROR_PRESETS = {
    "cn": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "tuna": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple/",
    "tencent": "https://mirrors.cloud.tencent.com/pypi/simple/",
    "ustc": "https://pypi.mirrors.ustc.edu.cn/simple/",
    "none": None,
    "off": None,
    "official": None,
}


def resolve_mirror(raw: str | None) -> str | None:
    """把 `--mirror` 的值规整成 pip index URL（或 None=官方源）。

    接受预设名（cn / tuna / aliyun / tencent / ustc / none）或直接给 URL。
    """
    v = (raw or "").strip()
    if not v:
        return _MIRROR_PRESETS["cn"]
    low = v.lower()
    if low in _MIRROR_PRESETS:
        return _MIRROR_PRESETS[low]
    if not v.startswith(("http://", "https://")):
        log(f"  ! --mirror `{v}` 既不是预设名也不是 URL，按官方源处理。"
            f"可用预设：{', '.join(sorted(k for k in _MIRROR_PRESETS if k != 'none'))}")
        return None
    return v

_DEMO_SKILL_MD = (
    b"---\nname: demo\nversion: 1.0.0\n"
    b"description: a demo skill doing useful everyday work\n---\nbody\n"
)

_EXPECT_TOOLS = sorted([
    "skillos_list_skills", "skillos_get_skill", "skillos_status",
    "skillos_check", "skillos_version_log", "skillos_sync_push",
    "skillos_sync_pull", "skillos_upgrade",
])

_CLI_SRC = "from skillos.cli.main import app; app()"
_MCP_SRC = "from skillos.mcp.server import main; main()"

_failures: list[str] = []
_checks = 0


def log(msg: str = "") -> None:
    print(msg, flush=True)


def ok(cond: bool, label: str) -> bool:
    global _checks
    _checks += 1
    if cond:
        log(f"  ok   - {label}")
    else:
        log(f"  FAIL - {label}")
        _failures.append(label)
    return bool(cond)


def platform_key() -> str:
    sysname = _platform.system().lower()
    mach = (_platform.machine() or "").lower()
    arch = "x64" if mach in ("amd64", "x86_64") else (
        "arm64" if mach in ("arm64", "aarch64") else mach)
    if sysname.startswith("win"):
        return f"win-{arch}"
    if sysname == "darwin":
        return f"mac-{arch}"
    return f"linux-{arch}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def req_name(line: str) -> str:
    """从 requirements 行里抽出归一化包名（PEP 503）：`pydantic[email]>=2.7` → `pydantic`。"""
    return re.split(r"[=<>!~;\[\] ]", line.strip(), 1)[0].replace("_", "-").lower()


# ---------------------------------------------------------------- 进程工具

def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


# 这两个 fetcher 都必须**把 HTTPError 当成一次正常响应**读回来。
# 血泪点：`urllib.error.HTTPError` 是 `URLError` 的子类 —— 若只 catch 后者，
# 任何 4xx/5xx 都会被当成「连接失败」吞成 (None, "", "")，于是
# `st == 404` 这类断言永远为假（实测：/api 未知路径的 JSON 404 就是这样假失败）。
def http_json(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, _parse_json(e)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None, None


def http_text(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.headers.get("content-type", ""), \
                r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", ""), _read_text(e)
    except (urllib.error.URLError, OSError):
        return None, "", ""


def _read_text(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""


def _parse_json(e: urllib.error.HTTPError):
    try:
        return json.loads(_read_text(e))
    except (json.JSONDecodeError, ValueError):
        return None


def wait_health(base: str, seconds: float = 45.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        st, _ = http_json(f"{base}/api/health")
        if st == 200:
            return True
        time.sleep(0.5)
    return False


def find_python_exe(root: Path) -> Path | None:
    for pat in _PY_GLOBS:
        for p in sorted(root.glob(pat)):
            if p.is_file():
                return p
    return None


# ---------------------------------------------------------------- [0] 形态与保密

def check_shape(dist: Path) -> dict:
    log("\n[0/4] 发行形态与保密自检")
    # 交付物是「一个 skill 包」：SKILL.md / scripts / manifest 都在 dist 根
    ok((dist / "SKILL.md").is_file(), f"SKILL.md 在包根（{dist / 'SKILL.md'}）")
    ok((dist / "scripts" / "start.py").is_file(), "scripts/start.py 在包内")
    mpath = dist / "manifest.json"
    if not ok(mpath.is_file(), f"manifest.json 在包根（{mpath}）"):
        return {}
    man = json.loads(mpath.read_text(encoding="utf-8"))
    ok(man.get("mode") == "dist", "manifest.mode == dist（build.py 已改写）")
    frozen = man.get("frozen") or {}
    if not ok(bool(frozen.get("dir")), "manifest.frozen 块存在（D1 形态标记）"):
        return {}
    ok(str(frozen.get("dir")).startswith("runtime/"),
       f"frozen.dir 指向包内载荷（{frozen.get('dir')}）")
    ok("requirements" in frozen, "manifest.frozen.requirements 已声明")
    pin = (man.get("runtimes", {}).get("python") or {}).get("pin")
    ok(bool(pin), f"manifest.runtimes.python.pin 已声明（{pin}）")

    # 运行时目录（包内 runtime/skillos，按 manifest 声明解析）
    runtime = (dist / str(frozen["dir"])).resolve()
    if not ok(runtime.is_dir(), f"运行时目录存在（{runtime}）"):
        return {}
    exts = [p for p in runtime.iterdir() if p.suffix in (".pyd", ".so")]
    ok(bool(exts), f"运行时里有编译产物（{[p.name for p in exts]}）")

    # 保密红线 1：运行时目录不得有 .py 源码
    py_leak = [str(p.relative_to(dist)) for p in runtime.rglob("*.py")]
    ok(not py_leak, f"运行时目录无 .py 源码（发现 {len(py_leak)} 个：{py_leak[:3]}）")
    pyc_leak = [p for p in runtime.rglob("__pycache__")]
    ok(not pyc_leak, f"运行时目录无 __pycache__（{len(pyc_leak)} 个）")

    # 保密红线 2：前端产物不得带 sourcemap
    web = dist / "runtime" / "web"
    maps = list(web.rglob("*.map")) if web.is_dir() else []
    ok(not maps, f"runtime/web/ 无 sourcemap（发现 {len(maps)} 个）")

    # 依赖清单（按 manifest 声明解析，回落默认位置）
    req = (dist / str(frozen.get("requirements") or "runtime/requirements.txt")).resolve()
    if not req.is_file():
        req = dist / "runtime" / "requirements.txt"
    ok(req.is_file(), f"第三方依赖清单存在（{req}）")
    if req.is_file():
        lines = [x.strip() for x in req.read_text(encoding="utf-8").splitlines()
                 if x.strip() and not x.lstrip().startswith("#")]
        ok(len(lines) >= 8, f"依赖清单条目合理（{len(lines)} 条）")
        ok(not any(req_name(x) == "skillos" for x in lines),
           "依赖清单里没有 skillos（frozen 形态不该装它）")

    # 便携运行时（包内 vendor/<平台>/python）
    rt = dist / "vendor" / platform_key()
    rpy = find_python_exe(rt / "python") if (rt / "python").is_dir() else None
    ok(rpy is not None, f"便携 CPython 随包（{rpy}）")

    # 入口脚本
    entry = dist / ("start.cmd" if os.name == "nt" else "start.sh")
    ok(entry.is_file(), f"零安装入口脚本存在（{entry.name}）")

    return {"manifest": man, "frozen": frozen, "runtime": runtime,
            "requirements": req, "runtime_python": rpy}


# ---------------------------------------------------------------- [1] 运行时与依赖

def _drop_dir(p: Path) -> None:
    """删除目录；被环境的安全策略拦下时给出可读指引，而不是裸堆栈。"""
    try:
        shutil.rmtree(p)
    except Exception as e:  # noqa: BLE001 —— 安全删除钩子可能抛任意类型
        log(f"  ! 无法删除 {p}：{e}")
        log("      → 请手工删除该目录后重跑（部分环境会拦截批量删除）。")
        sys.exit(1)


def _venv_python_works(vpy: Path) -> bool:
    """冒烟 venv 的解释器**真的能跑**吗。

    坑：venv 的 `pyvenv.cfg` 把基座解释器路径写死了。发行目录一旦换位置
    （本次把载荷从 `<dist>/skillos` 挪到 `<dist>/runtime/skillos`，整个 dist
    目录名也变了），旧 venv 就会报
        `No Python at '<旧路径>/python.exe'`
    而 `vpy.exists()` 仍为真 —— 只看文件存在会一直“复用”一个跑不起来的 venv。
    """
    if not vpy.is_file():
        return False
    try:
        p = subprocess.run([str(vpy), "-c", "print('ok')"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


def ensure_smoke_venv(rt_py: Path, req: Path, mirror: str | None, rebuild: bool) -> Path | None:
    """用包内自带的 CPython 建 venv 并装 requirements。按 requirements 哈希缓存。

    刻意**不做无谓 rmtree**：pip 本身就是幂等的，重复 `install -r` 只会补齐差异，
    比"删了重建"更快也更安全。只有 venv 已经跑不起来时才删。
    """
    SMOKE.mkdir(parents=True, exist_ok=True)
    venv = SMOKE / "venv"
    vpy = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    stamp = SMOKE / "requirements.sha256"
    want = sha256_text(req.read_text(encoding="utf-8") + "|" + str(rt_py))

    if rebuild and venv.exists():
        _drop_dir(venv)

    alive = _venv_python_works(vpy)
    if alive and not rebuild and stamp.is_file() and \
            stamp.read_text(encoding="utf-8").strip() == want:
        log(f"  ok   - 复用冒烟 venv（requirements 未变）：{venv}")
        return vpy

    if not alive:
        if venv.exists():
            log(f"  ..   冒烟 venv 已失效（基座解释器路径变了），删除重建：{venv}")
            _drop_dir(venv)
        log(f"  ..   用便携 CPython 建 venv：{venv}")
        rc = subprocess.run([str(rt_py), "-m", "venv", str(venv)],
                            capture_output=True, text=True)
        if not ok(rc.returncode == 0 and vpy.exists(),
                  f"`{rt_py.name} -m venv` 成功（rc={rc.returncode}）"):
            log("      " + "\n      ".join((rc.stderr or "").splitlines()[-10:]))
            return None
    else:
        log(f"  ..   复用已有 venv，按 requirements 收敛差异：{venv}")

    log(f"  ..   安装第三方依赖（{mirror or '官方源'}）…")
    cmd = [str(vpy), "-m", "pip", "install", "-r", str(req), "--disable-pip-version-check"]
    if mirror:
        cmd += ["-i", mirror]
    t0 = time.time()
    rc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if not ok(rc.returncode == 0, f"pip install -r requirements.txt（rc={rc.returncode}）"):
        log("      " + "\n      ".join((rc.stderr or "").splitlines()[-20:]))
        return None
    log(f"  ok   - 依赖安装完成（{time.time() - t0:.0f}s）")
    stamp.write_text(want, encoding="utf-8")
    return vpy


def probe_import(vpy: Path, runtime: Path) -> bool:
    """断言 `import skillos` 命中的是**编译产物**，不是某个源码目录。"""
    env = {**os.environ, "PYTHONPATH": str(runtime)}
    code = (
        "import skillos, json\n"
        "comp = getattr(skillos, '__compiled__', None)\n"
        "print(json.dumps({\n"
        "  'file': skillos.__file__,\n"
        "  'origin': getattr(getattr(skillos, '__spec__', None), 'origin', None),\n"
        "  'loader': type(getattr(skillos, '__loader__', None)).__name__,\n"
        "  'ext': getattr(comp, 'extension_filename', None),\n"
        "}))\n"
    )
    p = subprocess.run([str(vpy), "-P", "-c", code], capture_output=True, text=True,
                       env=env, timeout=120)
    if not ok(p.returncode == 0, f"`import skillos` 成功（rc={p.returncode}）"):
        log("      " + "\n      ".join((p.stderr or "").splitlines()[-10:]))
        return False
    info = json.loads((p.stdout or "{}").strip().splitlines()[-1])
    f = str(info.get("file") or "")
    origin = str(info.get("origin") or "")
    loader = str(info.get("loader") or "")
    ext = str(info.get("ext") or "")
    log(f"      skillos.__file__          = {f}")
    log(f"      skillos.__spec__.origin   = {origin}")
    log(f"      skillos.__loader__        = {loader}")
    log(f"      __compiled__.extension_filename = {ext}")
    # **不要用 __file__ / __spec__.origin 的后缀判断**（实测踩坑）。
    # `--mode=package` 下 Nuitka 为了让包内子模块还能按路径解析，会把
    # __file__ 与 __spec__.origin 都改写成**虚拟源码路径**：
    #     <pyd 所在目录>/<包名>/<原相对路径>      ← 磁盘上并没有这个 .py
    # 所以正确的证据只有 Nuitka 自己注入的编译标记：
    #   __loader__ 名含 nuitka；__compiled__.extension_filename 指向真 .pyd/.so
    ok(not (f.endswith(".py") and Path(f).is_file()) if f else True,
       "运行时目录里没有对应源码文件（__file__ 是虚拟路径）")
    ok("nuitka" in loader.lower(),
       f"skillos 由 Nuitka 编译产物加载（__loader__={loader}）")
    ok(bool(ext) and ext.endswith((".pyd", ".so")) and Path(ext).is_file(),
       f"编译产物真实存在且为 .pyd/.so（{ext or '缺失 __compiled__'}）")
    return True


# ---------------------------------------------------------------- [2] CLI

def run_env(runtime: Path, home: Path, **extra) -> dict:
    """子进程环境：把发行物当**真用户环境**用，且完全沙盒化。

    `SKILLOS_ROOT` 与 `SKILLOS_HOME` **两个都设**：入口层现在已统一收敛到
    `resolve_home()`（`SKILLOS_ROOT` 优先，`SKILLOS_HOME` 为兼容别名），设两个
    是为了同时对旧调用方也保持隔离。只设错的那一个，就会退回 `Path.home()`，
    于是冒烟会去扫开发机的真实 `~/.claude/skills` / `~/.workbuddy/skills` ——
    实测撞出 arch-designer / bid-capture 的 slug 冲突、`skillos import` 直接退 3。
    """
    return {
        **os.environ,
        "PYTHONPATH": str(runtime),
        "SKILLOS_ROOT": str(home),
        "SKILLOS_HOME": str(home),
        **extra,
    }


def check_cli(vpy: Path, runtime: Path, home: Path) -> None:
    log("\n[2/4] CLI")
    env = run_env(runtime, home)
    p = subprocess.run([str(vpy), "-c", _CLI_SRC, "--help"],
                       capture_output=True, text=True, env=env, timeout=180)
    ok(p.returncode == 0, f"`skillos --help` 退出码 0（实际 {p.returncode}）")
    out = (p.stdout or "") + (p.stderr or "")
    for cmd in ("init", "daemon", "mcp", "push", "check"):
        ok(cmd in out, f"`--help` 列出子命令 `{cmd}`")
    if p.returncode != 0:
        log("      stderr 尾部：")
        log("      " + "\n      ".join((p.stderr or "").splitlines()[-15:]))
    if p.stdout:
        log("      " + "\n      ".join(p.stdout.splitlines()[:6]))


def seed_home(vpy: Path, runtime: Path, home: Path) -> bool:
    """用**发行物自己**初始化环境并纳管一个 demo skill。"""
    d = home / ".claude" / "skills" / "demo"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_bytes(_DEMO_SKILL_MD)

    env = run_env(runtime, home)
    p1 = subprocess.run([str(vpy), "-c", _CLI_SRC, "init"], env=env,
                        capture_output=True, text=True, timeout=300)
    if not ok(p1.returncode == 0, f"`skillos init` 退出码 0（实际 {p1.returncode}）"):
        log("      " + "\n      ".join((p1.stderr or "").splitlines()[-20:]))
        return False
    p2 = subprocess.run([str(vpy), "-c", _CLI_SRC, "import"], env=env,
                        capture_output=True, text=True, timeout=300)
    if not ok(p2.returncode == 0, f"`skillos import` 退出码 0（实际 {p2.returncode}）"):
        log("      " + "\n      ".join((p2.stderr or "").splitlines()[-20:]))
        return False
    return True


# ---------------------------------------------------------------- [3] MCP

def check_mcp(vpy: Path, runtime: Path, home: Path) -> None:
    log("\n[3/4] MCP stdio")
    if not seed_home(vpy, runtime, home):
        ok(False, "MCP 前置（init + import）失败，跳过协议断言")
        return

    env = run_env(runtime, home)
    proc = subprocess.Popen([str(vpy), "-c", _MCP_SRC], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    lines: list[str] = []
    errs: list[str] = []

    def reader():
        assert proc.stdout is not None
        for raw in proc.stdout:
            lines.append(raw.decode("utf-8", "replace").strip())

    def ereader():
        assert proc.stderr is not None
        for raw in proc.stderr:
            errs.append(raw.decode("utf-8", "replace").rstrip())

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=ereader, daemon=True).start()

    def send(msg: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def wait(req_id: int, timeout: float = 30.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for ln in list(lines):
                if not ln:
                    continue
                try:
                    m = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if m.get("id") == req_id:
                    return m
            if proc.poll() is not None:
                return None
            time.sleep(0.2)
        return None

    counter = {"n": 10}

    def call(tool: str, args: dict) -> dict | None:
        rid = counter["n"]
        counter["n"] += 1
        send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
              "params": {"name": tool, "arguments": args}})
        r = wait(rid)
        if r is None:
            return None
        try:
            return json.loads(r["result"]["content"][0]["text"])
        except (KeyError, IndexError, json.JSONDecodeError):
            return None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "skillos-smoke", "version": "0"}}})
        init = wait(1)
        if init is None:
            ok(False, "MCP initialize 有响应")
            log("      stderr 尾部：")
            log("      " + "\n      ".join(errs[-20:]))
            return
        ok(init["result"]["serverInfo"]["name"] == "skillos",
           "MCP initialize（serverInfo=skillos）")

        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = wait(2)
        names = sorted(t["name"] for t in (listed or {}).get("result", {}).get("tools", []))
        ok(names == _EXPECT_TOOLS, f"八工具白名单（OUT-14）实际 {len(names)} 个")

        r = call("skillos_list_skills", {})
        ok(bool(r and r.get("ok")), "tools/call list_skills")
        r = call("skillos_sync_push", {"slug": "demo"})
        ok(bool(r and r.get("dry_run") is True), "push 默认 dry_run（FR-1.3.3）")
        r = call("skillos_upgrade", {"slug": "demo"})
        ok(bool(r and not r.get("ok")
                and r.get("error", {}).get("code") == "NOT_IMPLEMENTED"),
           "upgrade 显式 NOT_IMPLEMENTED（红线 7）")
        ok((home / ".claude" / "skills" / "demo" / "SKILL.md").read_bytes() != b"",
           "dry_run 未破坏源文件")
    finally:
        kill_tree(proc)


# ---------------------------------------------------------------- [4] Server

def check_server(vpy: Path, runtime: Path, dist: Path, home: Path, port: int) -> None:
    log("\n[4/4] 后端 ASGI + SPA 托管")
    base = f"http://127.0.0.1:{port}"
    env = run_env(
        runtime, home,
        SKILLOS_HOST="127.0.0.1",
        SKILLOS_PORT=str(port),
        SKILLOS_JWT_SECRET="smoke-fixed-secret-not-a-real-credential",
        SKILLOS_DB_URL=f"sqlite:///{(home / 'cloud.db').as_posix()}",
        SKILLOS_WEB_DIST=str(dist / "runtime" / "web"),
    )
    argv = [str(vpy), "-m", "uvicorn", "skillos.api.asgi:app",
            "--host", "127.0.0.1", "--port", str(port)]
    logp = home / "server.log"
    with open(logp, "wb") as fh:
        proc = subprocess.Popen(argv, env=env, stdout=fh, stderr=fh)
    try:
        if not ok(wait_health(base, seconds=90), f"/api/health 通（{base}）"):
            log("      日志尾部：")
            tail = logp.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
            log("      " + "\n      ".join(tail))
            return
        st, _ = http_json(f"{base}/api/health")
        ok(st == 200, "/api/health 返回 200")
        st, ctype, body = http_text(f"{base}/")
        ok(st == 200 and "html" in ctype.lower(), f"/ 返回 HTML（{st} {ctype}）")
        ok("id=\"root\"" in body or "<div" in body, "SPA 挂载点存在（web/ 随包生效）")
        st, _, _ = http_text(f"{base}/skills/not-a-real-route")
        ok(st == 200, "SPA 回退：未知前端路由不 404")
        st, _, b2 = http_text(f"{base}/api/not-a-real-endpoint")
        ok(st == 404 and b2.strip().startswith("{"),
           "/api 未知路径返回 JSON 404（不被 SPA 吃掉）")
    finally:
        kill_tree(proc)


# ---------------------------------------------------------------- 主流程

def main() -> None:
    ap = argparse.ArgumentParser(description="D1 编译产物四重冒烟")
    ap.add_argument("--dist", default=None,
                    help="skill 包目录（默认 build/dist/skillos-<当前平台>）")
    ap.add_argument("--port", type=int, default=9288, help="后端冒烟端口")
    ap.add_argument("--mirror", default="cn",
                    help="pip 源：预设 cn / tuna / aliyun / tencent / ustc / none，"
                         "或直接给 index URL（默认 cn=清华）")
    ap.add_argument("--rebuild-venv", action="store_true", help="强制重建冒烟 venv")
    ap.add_argument("--keep", action="store_true", help="保留临时 home 目录")
    args = ap.parse_args()

    dist = Path(args.dist).resolve() if args.dist else (DIST_ROOT / f"skillos-{platform_key()}")
    if not dist.is_dir():
        log(f"找不到 skill 包目录：{dist}")
        log("先执行：python build/build.py")
        sys.exit(2)

    log(f"=== 编译产物冒烟 · {dist} ===")
    shape = check_shape(dist)
    if not shape:
        log("\n发行形态不正确，无法继续。")
        sys.exit(2)

    rpy = shape.get("runtime_python")
    if rpy is None:
        log("\n没有便携 CPython —— 退回用当前解释器建 venv（不推荐，仅用于构建机自检）。")
        rpy = Path(sys.executable)

    mirror = resolve_mirror(args.mirror)

    log("\n[1/4] 运行时与依赖")
    vpy = ensure_smoke_venv(rpy, shape["requirements"], mirror, args.rebuild_venv)
    if vpy is None:
        log("\n依赖安装失败，无法继续。")
        sys.exit(1)
    probe_import(vpy, shape["runtime"])

    tmp = Path(tempfile.mkdtemp(prefix="skillos-d1-smoke-"))
    log(f"      临时 home（SKILLOS_ROOT / SKILLOS_HOME）：{tmp}")
    try:
        check_cli(vpy, shape["runtime"], tmp)
        check_mcp(vpy, shape["runtime"], tmp)
        check_server(vpy, shape["runtime"], dist, tmp, args.port)
    finally:
        if args.keep:
            log(f"\n保留临时目录：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    log("")
    if _failures:
        log(f"=== 冒烟失败：{len(_failures)}/{_checks} 项未通过 ===")
        for f in _failures:
            log(f"  - {f}")
        sys.exit(1)
    log(f"=== 全部通过 ✔ （{_checks} 项断言，D1 发行物自证完成）===")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        print(f"smoke: 未预期错误：{e}", file=sys.stderr, flush=True)
        sys.exit(1)
