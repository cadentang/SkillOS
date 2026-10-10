"""Skill OS 编译发行驱动（第二步：Nuitka 编译管线）。

两条路线（由 build/nuitka.json 的顶层 `route` 选择）
---------------------------------------------------
* **module（默认，D1）** —— 只把 `skillos/` 自身编成扩展模块（`.pyd` / `.so`），
  第三方依赖全部 `--nofollow-import-to` 排除，运行期由随包 venv 提供。
  实测：冷编译 10 分 50 秒，产物 4.5 MB，机器码（源码不可见）。
* **standalone（退路，A）** —— 把第三方一并编进去，产出完全自足的发行物。
  代价：约 2.5 小时 / 1637 个模块。保留为退路，切换只需改 JSON。

设计约束
--------
1. **纯标准库**。驱动要能在「还没装 Nuitka / 还没装项目依赖」的裸机构建机上
   起步并给出可读指引，不能自己先崩在 import 上。
2. **规格驱动**。所有 include / exclude / 平台差异都读 build/nuitka.json，
   本文件不含任何业务性硬编码 —— 换形态只改 JSON。
3. **不假报成功**。每个阶段要么拿到可验证的产物（文件存在 + 可执行 + sha256），
   要么非零退出并打印 Nuitka 输出尾部。绝不允许「调用了命令就算构建完成」。

用法
----
    python build/build.py --list                    # 列出路线与规格
    python build/build.py --dry-run                 # 只打印将要执行的命令
    python build/build.py                           # 全量构建 + 装配
    python build/build.py --route standalone        # 切到退路路线跑
    python build/build.py --skip-build              # 复用 build/out，只重新装配
    python build/build.py --vendor-python <目录>    # 指定要随包的便携 CPython
    python build/build.py --clean                   # 先清 build/out 与 build/dist

产物（module 路线）
------------------
    build/out/module/skillos.cp311-win_amd64.pyd   编译中间物（也会进发行物）
    build/dist/<platform>/                         发行目录（最终物）
        ├── skillos/           .pyd + 数据目录（PATH 注入点，源码不可见）
        ├── requirements.txt   第三方依赖清单（launcher 装它，不装 skillos）
        ├── web/               前端预构建产物（零 Node）
        ├── launcher/          启动器（含 vendor/<平台>/python 便携运行时）
        ├── start.cmd          零 Python 用户的双击入口
        ├── start.sh
        ├── BUILD-INFO.json
        └── README.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build"
OUT = BUILD / "out"
DIST = BUILD / "dist"


# ---------------------------------------------------------------- 基础工具

def log(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"  ! {msg}", flush=True)


def die(msg: str, code: int = 1):
    print(f"\n构建中止：{msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def load_spec() -> dict:
    p = BUILD / "nuitka.json"
    if not p.exists():
        die(f"找不到构建规格：{p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"构建规格不是合法 JSON：{p}\n  {e}")


def platform_key() -> str:
    """win-x64 / mac-arm64 / mac-x64 / linux-x64 / linux-arm64"""
    sysname = platform.system().lower()
    mach = (platform.machine() or "").lower()
    if mach in ("amd64", "x86_64"):
        arch = "x64"
    elif mach in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = mach or "unknown"
    if sysname.startswith("win"):
        return f"win-{arch}"
    if sysname == "darwin":
        return f"mac-{arch}"
    return f"linux-{arch}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_stats(path: Path) -> dict:
    """目录的体积与文件数（不逐个列 sha256 —— 运行时动辄几千个文件）。"""
    n = 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                n += 1
                total += p.stat().st_size
        except OSError:
            continue
    return {"files": n, "bytes": total}


def human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.1f}{unit}"
        f /= 1024
    return f"{f:.1f}GB"


def canonical(name: str) -> str:
    """PEP 503 归一化：比较包名时用（SQLAlchemy / sqlalchemy / sql_alchemy 等价）。"""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def standalone_targets(spec: dict) -> list[dict]:
    """standalone 路线的 target 列表（module 路线为空）。"""
    return (spec.get("standalone") or {}).get("targets") or []


def normalize_version(text: str | None, default: str = "0.1.0") -> str:
    """把 tag（如 `v1.2.3`）规整成 `1.2.3`。

    Windows 的资源版本号必须是纯数字四段，`v1.2.3` 直接塞会构建失败，
    所以在入口处一次收敛，别让 CI 的 tag 命名规则漏进产物元数据。
    """
    raw = (text or "").strip().lstrip("vV")
    parts: list[str] = []
    for chunk in raw.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(digits)
    return ".".join(parts) if parts else default


def run(cmd: list[str], cwd: Path | None = None, dry: bool = False,
        tail_lines: int = 40) -> int:
    printable = " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd)
    prefix = "[dry-run] " if dry else ""
    log(f"{prefix}$ {printable}")
    if dry:
        return 0
    # 非交互护栏 —— 这是实测踩出来的硬需求，不是优化项。
    #
    # Nuitka 在需要下载辅助工具（winlibs MinGW64 / Dependency Walker / ccache）
    # 时会**交互式提问**：`... Proceed and download? [Yes]/No :`。若 stdin 是不可
    # 读的管道或本就无人应答，queryUser() 会一直阻塞在读取上 → **构建永久挂起**
    # （实测空转 18 分钟、零产物、零报错，极难定位）。
    #
    # `--assume-yes-for-downloads` 能覆盖这些下载，但依赖上游对该开关的传递是否
    # 完整；这里再兜一层：喂一串 `yes`，任何残留提问都会被自动确认。
    # 即使没有提问，多余输入也只是被忽略 —— **绝无挂死可能**。
    proc = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        input="yes\n" * 64, text=True,
    )
    if proc.returncode != 0:
        log(f"\n!! 命令以 {proc.returncode} 退出")
    return proc.returncode


# ---------------------------------------------------------------- 环境校验

def check_python(spec: dict) -> None:
    cur = f"{sys.version_info.major}.{sys.version_info.minor}"
    lo = spec["python"]["min"]
    hi = spec["python"].get("max_exclusive")
    if cur < lo or (hi and cur >= hi):
        die(f"构建机 Python {cur} 不在支持区间 [{lo}, {hi or '∞'})。\n"
            f"      → Nuitka 产物绑定构建机的 Python 版本，必须用目标版本构建。")
    if spec.get("route") == "module":
        pin = spec["python"].get("pin")
        if pin and cur != pin:
            die(f"route=module 要求构建机 Python **恰好是 {pin}**（当前 {cur}）。\n"
                f"      根因：发行物是 `{spec['module']['package']}."
                f"{spec['module'].get('python_tag', 'cp???')}-*.pyd`，只认 CPython {pin} 的 ABI。\n"
                f"      三选一：\n"
                f"        1) 用 Python {pin} 建构建环境（推荐；本机与 CI 都这么钉）\n"
                f"        2) 把 build/nuitka.json 的 route 改成 standalone（不做 ABI 锁，代价是 2.5 小时冷构建）\n"
                f"        3) 交给 CI：.github/workflows/release.yml")


def check_nuitka(spec: dict, dry: bool) -> str | None:
    """取 Nuitka 版本 —— **刻意不用 `python -m nuitka --version`**。

    实测：`--version` 在缺编译器时会顺带探测 C 编译器、进而尝试下载 winlibs
    MinGW64 并**弹出交互提问**。在非交互环境里这就是一次挂起（本管线踩过）。
    版本号完全可以零副作用地拿到 —— 读包元数据即可。
    """
    if dry:
        return None
    try:
        from importlib.metadata import version as _pkg_version

        ver = _pkg_version("nuitka")
    except Exception:  # noqa: BLE001 —— 元数据缺失（非常规安装）才退回子进程
        try:
            p = subprocess.run([sys.executable, "-m", "nuitka", "--version"],
                               capture_output=True, text=True, timeout=180,
                               input="yes\n" * 4)
        except (OSError, subprocess.SubprocessError) as e:
            die(f"无法确定 Nuitka 版本：{e}\n"
                f"      → 确认已安装：{sys.executable} -m pip install nuitka")
        if p.returncode != 0:
            die("未安装 Nuitka。先执行：\n"
                f"      {sys.executable} -m pip install nuitka\n"
                "  （构建环境请与项目依赖隔离，不要污染系统 Python）")
        ver = (p.stdout or "").strip().splitlines()[0] if p.stdout.strip() else "unknown"
    log(f"  Nuitka: {ver}")
    return ver


def check_entrypoints(spec: dict) -> None:
    """只对 standalone 路线校验入口脚本（module 路线的输入是整个包，没有入口文件）。"""
    if spec.get("route") == "module":
        pkg = REPO / spec["module"]["package"]
        if not (pkg / "__init__.py").exists():
            die(f"route=module 但找不到要编译的包：{pkg}（需含 __init__.py）")
        return
    for t in standalone_targets(spec):
        p = REPO / t["entry"]
        if not p.exists():
            die(f"target `{t['name']}` 的入口不存在：{p}")


def check_data_files(spec: dict) -> None:
    for df in spec["include"].get("data_files", []):
        p = REPO / df["src"]
        if not p.exists():
            die(f"声明要打包的数据文件不存在：{p}\n"
                "      → 数据文件漏打包会在运行期才炸（Path(__file__) 找不到），构建期拦住。")


# ---------------------------------------------------------------- 编译器决策

def check_compiler(spec: dict, pk: str, dry: bool) -> str:
    """Windows 上决定用哪个编译器，并在**秒级**拦住不可能的组合。

    为什么需要这一步（实测得来，不是纸上推演）：
      * Nuitka 4.x 在 **Python ≥ 3.13 上禁用 `--mingw64`**：
        `FATAL: Error, cannot use '--mingw64' on Python version 3.13 or higher.`
        —— 3.13+ 只有 MSVC 一条路（上游尚未提供 3.13 的 MinGW 构建）。
      * 没有编译器时 Nuitka 会**交互式提问**（依赖 Dependency Walker /
        MinGW64 的下载），非交互环境里表现为**永久挂起**：实测空转 14 分钟、
        零产物、零下载。所以 `--assume-yes-for-downloads` 是硬要求（已在命令行里）。
        这里再兜一层：编译器组合不合法时立刻报错，而不是让构建卡住。

    决策表（Windows）：
        Python ≤ 3.12 → mingw64（自带下载，无需装 Visual Studio）
        Python ≥ 3.13 → msvc   （需已装 VS Build Tools）
    显式指定了不合法组合（如 3.13 + mingw64）→ 报错，**不静默改写**。
    """
    plat = spec.get("platforms", {}).get(pk, {})
    want = spec.get("_compiler") or plat.get("compiler") or "auto"
    if not pk.startswith("win"):
        return "auto"

    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    mingw_ok = (sys.version_info.major, sys.version_info.minor) <= (3, 12)

    if not mingw_ok and want == "mingw64":
        msg = (
            f"Nuitka 在 Python ≥ 3.13 上禁用 --mingw64（上游未提供 3.13 的 MinGW 构建）。\n"
            f"      当前构建机 Python：{ver}\n"
            f"      三选一：\n"
            f"        1) 装 Visual Studio Build Tools，然后 `--compiler msvc`\n"
            f"           （CI 的 windows-latest 自带 MSVC，无需处理 —— release.yml 已传该参数）\n"
            f"        2) 用 Python 3.11 / 3.12 构建（3.12 及以下仍可 --mingw64，无需装 VS）\n"
            f"        3) 交给 CI 构建：.github/workflows/release.yml"
        )
        if dry:
            log(f"  ! [dry-run] 编译器组合不合法，真构建会中止：\n      {msg}")
            return "mingw64"
        die(msg)

    if want == "auto":
        return "mingw64" if mingw_ok else "msvc"
    return want


def _compiler_flags(spec: dict, pk: str, cmd: list[str]) -> None:
    if not pk.startswith("win"):
        return
    comp = spec.get("_resolved_compiler") or \
        spec.get("platforms", {}).get(pk, {}).get("compiler") or "auto"
    if comp == "mingw64":
        cmd.append("--mingw64")
    elif comp == "msvc":
        cmd.append("--msvc=latest")


# ---------------------------------------------------------------- 开关校验

_HELP_CACHE: str | None = None


def nuitka_help() -> str:
    global _HELP_CACHE
    if _HELP_CACHE is None:
        # stdin 指向空设备：`--help` 本不该提问，万一提问也立刻失败而非挂起。
        p = subprocess.run([sys.executable, "-m", "nuitka", "--help"],
                           capture_output=True, text=True, timeout=300,
                           stdin=subprocess.DEVNULL)
        _HELP_CACHE = (p.stdout or "") + (p.stderr or "")
    return _HELP_CACHE


def verify_flags(cmd: list[str], spec: dict) -> None:
    """用 `nuitka --help` 校验我们将发出的每个开关都真实存在。

    为什么必须做：Nuitka 会在小版本间**改名**（4.2.2 实测：`--standalone` 已被
    `--mode=standalone` 取代且旧名报 unknown option）。这类改动在安装期不暴露，
    只在构建时报错，而一次构建以十几分钟计 —— 在「命令行组装」阶段就拦住，
    代价接近零。加新开关后若 Nuitka 不支持，这里会立刻点名而不是等到编译中途。
    """
    known = set(re.findall(r"^\s*--([A-Za-z0-9][A-Za-z0-9_-]*)", nuitka_help(), re.M))
    if not known:
        return  # 拿不到帮助文本时不误伤（交给真实构建去暴露）
    bad = []
    for a in cmd:
        if not a.startswith("--"):
            continue
        name = a[2:].split("=", 1)[0]
        if name not in known:
            bad.append(a)
    if bad:
        die("以下开关在当前 Nuitka 上不存在（版本间改名的典型症状）：\n"
            + "\n".join(f"        {b}" for b in bad)
            + f"\n      当前 Nuitka：{spec.get('_nuitka_version')}\n"
              "      → 改 build/nuitka.json 或 build.py 的对应用法；"
              "完整开关表见 `python -m nuitka --help`。")


# ---------------------------------------------------------------- 排除项采集

def site_packages() -> Path | None:
    p = sysconfig.get_paths().get("purelib")
    if p and Path(p).is_dir():
        return Path(p)
    return None


def collect_exclude_packages(spec: dict) -> list[str]:
    """扫描构建 venv 的 site-packages，生成 module 路线的 --nofollow-import-to 清单。

    这是 D1 的核心开关：它把 1560 个第三方模块挡在编译之外，只保留运行期
    import 能力（由随包 venv 提供）。新增依赖会被自动收进来，无需手工维护。

    两道安全网：
      * `never_exclude`（skillos 自己）—— 构建 venv 里它以 editable 形式存在，会被扫到
      * `sys.stdlib_module_names` —— 万一 site-packages 里有跟标准库同名的包，
        排掉它会让 stdlib 也跟着消失，这类名字一律不排除
    """
    sp = site_packages()
    if sp is None:
        warn("拿不到 site-packages，排除清单为空（编译面会退化成全量，慎用）")
        return []
    keep = {canonical(n) for n in spec["module"].get("never_exclude", [])}
    stdlib = {canonical(n) for n in getattr(sys, "stdlib_module_names", set())}

    names: set[str] = set()
    for e in os.listdir(sp):
        p = sp / e
        if e.endswith((".dist-info", ".egg-info", ".pth", ".egg-link")) \
                or e.startswith("__pycache__"):
            continue
        if p.is_dir():
            if (p / "__init__.py").exists():
                names.add(e)
        elif p.suffix == ".py" and not e.startswith("_"):
            names.add(e[:-3])

    out: list[str] = []
    for n in sorted(names):
        c = canonical(n)
        if c in keep or c in stdlib:
            continue
        out.append(n)
    log(f"  排除第三方顶层包：{len(out)} 个（来自 {sp}）")
    return out


# ---------------------------------------------------------------- 命令行组装

def _standalone_cmd(spec: dict, t: dict, out_dir: Path, pk: str) -> list[str]:
    prod = spec["product"]
    # Windows 的文件版本号必须 4 段数字
    v = spec.get("_product_version") or "0.1.0"
    v4 = ".".join((v.split(".") + ["0"] * 4)[:4])

    cmd = [
        sys.executable, "-m", "nuitka",
        # Nuitka 2.8 起 `--standalone` / `--onefile` / `--module` 被 `--mode=` 取代，
        # 4.x 已移除旧名（实测 4.2.2：`--standalone` 直接 unknown option）。
        f"--mode={spec['standalone']['nuitka_mode']}",
        "--assume-yes-for-downloads",
        f"--output-dir={out_dir}",
        f"--output-filename={t['output_filename']}",
        f"--report={out_dir / (t['name'] + '.report.xml')}",
        f"--company-name={prod['company']}",
        f"--product-name={prod['display_name']}",
        f"--file-description={prod['description']}",
        f"--copyright={prod['copyright']}",
        f"--file-version={v4}",
        f"--product-version={v4}",
    ]
    for flag in spec.get("python_flags", []):
        cmd.append(f"--python-flag={flag}")
    for pkg in spec["standalone"]["include_packages"]:
        cmd.append(f"--include-package={pkg['name']}")
    for pkg in spec["include"].get("modules", []):
        cmd.append(f"--include-package={pkg}")
    for df in spec["include"].get("data_files", []):
        src = (REPO / df["src"]).as_posix()
        cmd.append(f"--include-data-files={src}={df['dest']}")
    for dist_name in spec["include"].get("distributions", []):
        cmd.append(f"--include-distribution-metadata={dist_name}")
    for ex in spec["exclude"]["packages"]:
        cmd.append(f"--nofollow-import-to={ex['name']}")
    _compiler_flags(spec, pk, cmd)
    if pk.startswith("win"):
        cmd.append(f"--windows-console-mode="
                   f"{spec['platforms'][pk].get('console_mode', 'attach')}")
    cmd.append(str(REPO / t["entry"]))
    return cmd


def _module_cmd(spec: dict, out_dir: Path, pk: str) -> list[str]:
    mod = spec["module"]
    pkg = mod["package"]

    cmd = [
        sys.executable, "-m", "nuitka",
        f"--mode={mod['nuitka_mode']}",     # "package"：整包编成一个扩展模块
        pkg,
        "--assume-yes-for-downloads",
        f"--output-dir={out_dir}",
        f"--report={out_dir / 'module.report.xml'}",
    ]
    if mod.get("force_include_package"):
        # package 模式已按包遍历；再叠一层强制收全，防包内「按字符串加载」的模块漏网。
        cmd.append(f"--include-package={pkg}")
    for flag in spec.get("python_flags", []):
        cmd.append(f"--python-flag={flag}")
    if mod.get("escape_deployment_flag"):
        # 关掉 Nuitka 的部署安全阀：D1 就是「故意排除第三方、运行期从 venv 取」，
        # 不关它会在运行期 ImportError: Module 'X' was actively excluded。
        cmd.append("--no-deployment-flag=excluded-module-usage")
    if mod.get("exclude_site_packages", True):
        # D1 的核心：第三方公开依赖不进机器码，运行期从随包 venv 取。
        # 规格开关 module.exclude_site_packages 控制此步（默认开）。
        for name in collect_exclude_packages(spec):
            cmd.append(f"--nofollow-import-to={name}")
    for ex in spec["exclude"]["packages"]:
        cmd.append(f"--nofollow-import-to={ex['name']}")
    # 去重（site-packages 扫描结果与 exclude.packages 可能重叠；重复开关无害但难读）
    seen: set[str] = set()
    deduped: list[str] = []
    for a in cmd:
        if a.startswith("--nofollow-import-to="):
            if a in seen:
                continue
            seen.add(a)
        deduped.append(a)
    cmd = deduped
    _compiler_flags(spec, pk, cmd)
    return cmd


def copy_data_files(spec: dict, out_dir: Path) -> list[Path]:
    """把数据文件按 <输出目录>/<dest> 落位。

    为什么不用 `--include-data-files`：**实测在 module/package 模式下它不落盘**
    （data 文件被跳过），只有 standalone 才生效。而 `__file__` 在扩展模块里是
    虚拟路径 `<输出目录>/<包名>/<原相对路径>`，所以数据必须落在同一棵树里。
    """
    done: list[Path] = []
    for df in spec["include"].get("data_files", []):
        src = REPO / df["src"]
        dst = out_dir / df["dest"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        done.append(dst)
    return done


# ---------------------------------------------------------------- 构建：module

def build_module(spec: dict, pk: str, dry: bool) -> Path | None:
    """route=module：编 skillos 成扩展模块，返回 .pyd/.so 路径。"""
    mod = spec["module"]
    pkg = mod["package"]
    out_dir = OUT / "module"
    cmd = _module_cmd(spec, out_dir, pk)
    if not dry:
        verify_flags(cmd, spec)
    rc = run(cmd, cwd=REPO, dry=dry)
    if dry:
        return None
    if rc != 0:
        report = out_dir / "module.report.xml"
        hint = f"\n  诊断报告：{report}" if report.exists() else ""
        die(f"skillos 扩展模块构建失败（Nuitka 退出码 {rc}）。{hint}\n"
            f"      → 常见根因：某个包按字符串加载了模块（漏 --include-package）、\n"
            f"        或 --mode=package 不被当前 Nuitka 支持（用 --help 核对）。")

    cands = sorted(list(out_dir.glob(f"{pkg}.cp*.pyd")) + list(out_dir.glob(f"{pkg}*.so")))
    if not cands:
        die(f"构建报告成功，但 {out_dir} 下找不到 {pkg}.cp*.pyd / {pkg}*.so —— 拒绝假报成功。")
    pyd = cands[-1]

    tag = mod.get("python_tag")
    if tag and tag not in pyd.name:
        die(f"产物名 `{pyd.name}` 里没有 {tag} —— 构建机的 Python 不是 "
            f"{spec['python'].get('pin')}。\n"
            f"      → 见 docs/10 §2「ABI 锁定」。")

    copied = copy_data_files(spec, out_dir)
    log(f"  ✓ {pyd.name}  {human(pyd.stat().st_size)}")
    for c in copied:
        log(f"  ✓ 数据文件 → {c.relative_to(OUT)}")
    return pyd


# ---------------------------------------------------------------- 构建：standalone

def build_target(spec: dict, t: dict, out_dir: Path, pk: str, dry: bool) -> Path | None:
    before = {p for p in out_dir.glob("*.dist")} if out_dir.exists() else set()
    cmd = _standalone_cmd(spec, t, out_dir, pk)
    if not dry:
        verify_flags(cmd, spec)
    rc = run(cmd, cwd=REPO, dry=dry)
    if dry:
        return None
    if rc != 0:
        report = out_dir / (t["name"] + ".report.xml")
        hint = f"\n  诊断报告：{report}" if report.exists() else ""
        die(f"target `{t['name']}` 构建失败（Nuitka 退出码 {rc}）。{hint}\n"
            f"      → 常见根因：某个库按字符串加载了模块（漏 --include-package）、\n"
            f"        或数据文件没随包（Path(__file__) 找不到）。")
    after = {p for p in out_dir.glob("*.dist")}
    new = sorted(after - before)
    if not new:
        # 增量构建时 .dist 已存在，回落到按名字找
        guess = out_dir / f"{Path(t['entry']).stem}.dist"
        new = [guess] if guess.exists() else sorted(after)
    if not new:
        die(f"target `{t['name']}` 报告成功，但找不到 *.dist 产物目录 —— 拒绝假报成功。")
    return new[-1]


def find_exe(dist_dir: Path, name: str) -> Path:
    for cand in (dist_dir / f"{name}.exe", dist_dir / name):
        if cand.is_file():
            return cand
    for p in dist_dir.iterdir():
        if p.is_file() and p.stem == name:
            return p
    die(f"产物目录里找不到可执行文件 `{name}`：{dist_dir}")


# ---------------------------------------------------------------- 依赖清单

def generate_requirements(spec: dict, dst: Path) -> list[str]:
    """生成送给 launcher 的运行时依赖清单。

    frozen 形态下 launcher 只装第三方、不装 skillos（skillos 由 .pyd 提供），
    所以清单里必须没有 skillos，也不该带 nuitka 这类构建工具。

    两种取法（规格里选）：
      * `source="pyproject"`（默认）—— 直接把 `[project].dependencies` 原样抄过来。
        **不 pin**。实测 `pip freeze` 会把构建机上的偶然版本一起固化（本次冻出了
        `attrs==26.1.0`，而源上最高只有 25.3.0），用户装到一半必然失败。
      * `source="freeze"` —— 构建 venv 的 freeze 减去构建期工具。可复现，但要求
        构建机装出来的版本在源上都可得。
    """
    rr = spec["runtime_requirements"]
    source = (rr.get("source") or "pyproject").lower()
    lines: list[str] = []

    if source == "freeze":
        rc = subprocess.run([sys.executable, "-m", "pip", "freeze"],
                            capture_output=True, text=True, timeout=300)
        if rc.returncode != 0:
            die(f"`pip freeze` 失败（退出码 {rc.returncode}）：\n{rc.stderr}")
        drop = {canonical(n) for n in rr.get("freeze_exclude", [])}
        for raw in (rc.stdout or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("-") or "@ file:" in line or " @ " in line:
                continue  # 可编辑安装 / 本地路径 / 直接 URL，装到用户机上是坏的
            name = re.split(r"[=<>!~ ]", line, 1)[0]
            if canonical(name) in drop:
                continue
            lines.append(line)
        header = ("# 由 build/build.py 生成：构建 venv 的 pip freeze"
                  "（已剔除构建期工具）。\n")
    else:
        py = REPO / "pyproject.toml"
        if not py.is_file():
            die(f"找不到 {py}，无法生成依赖清单。")
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover —— 3.11+ 都有
            die("需要 Python ≥ 3.11 的 tomllib 来读 pyproject.toml。")
        data = tomllib.loads(py.read_text(encoding="utf-8"))
        deps = (data.get("project") or {}).get("dependencies") or []
        if not deps:
            die("pyproject.toml 的 [project].dependencies 是空的。")
        lines = [str(d).strip() for d in deps if str(d).strip()]
        header = ("# 由 build/build.py 生成：抄自 pyproject.toml 的 [project].dependencies。\n"
                  "# 刻意不 pin —— 版本由区间约束 + pip 解析决定（见 build/nuitka.json 的说明）。\n")

    present = {canonical(re.split(r"[=<>!~ \[\]]", x, 1)[0]) for x in lines}
    missing = [n for n in rr.get("must_contain", []) if canonical(n) not in present]
    if missing:
        die(f"生成的 {rr['file']} 缺少关键依赖：{missing}\n"
            f"      → 说明 pyproject.toml 的依赖声明不全。先补声明再构建。")

    dst.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    log(f"  ✓ {dst.name}  {len(lines)} 个依赖（source={source}）")
    return lines


# ---------------------------------------------------------------- 便携运行时

_PY_GLOBS = ("python.exe", "*/python.exe", "bin/python3", "*/bin/python3", "bin/python")


def find_python_exe(root: Path) -> Path | None:
    for pat in _PY_GLOBS:
        for p in sorted(root.glob(pat)):
            if p.is_file():
                return p
    return None


def resolve_vendor_python_source(spec: dict, arg: str | None) -> Path | None:
    """确定随包的便携 CPython 从哪来。优先级：CLI > 环境变量 > uv 现场装。"""
    fr = spec["frozen_runtime"]
    ver = str(fr.get("python_version") or spec["python"].get("pin") or "3.11")
    for cand, label in ((arg, "--vendor-python"),
                        (os.environ.get("SKILLOS_VENDOR_PYTHON"), "SKILLOS_VENDOR_PYTHON")):
        if cand:
            p = Path(cand).expanduser().resolve()
            if not p.is_dir():
                die(f"{label} 指向的目录不存在：{p}")
            if find_python_exe(p) is None:
                die(f"{label} 目录里找不到 Python 解释器：{p}\n"
                    f"      → 需要形如 cpython-{ver}*/python.exe 的结构。")
            return p
    # 回落：现场用 uv 装一份（CI 有网时走这条）
    uv = shutil.which("uv")
    tmp = OUT / "vendor-python"
    if uv and not tmp.exists():
        log(f"  现场用 uv 预置 CPython {ver} → {tmp}")
        rc = subprocess.run([uv, "python", "install", ver, "--install-dir", str(tmp)],
                            capture_output=True, text=True, timeout=1800)
        if rc.returncode == 0 and find_python_exe(tmp):
            return tmp
        warn(f"uv 预置 CPython 失败：{(rc.stderr or '').strip().splitlines()[-1:] }")
    if tmp.is_dir() and find_python_exe(tmp):
        return tmp
    return None


def _python_install_root(src: Path) -> Path:
    """定位"安装根"：真正含解释器的那一层目录，且已解掉符号链接。

    实测坑：`uv python install` 会在 install-dir 下同时放**实体目录**
    （`cpython-3.11.17-...`）和一个**指向它的符号链接**（`cpython-3.11-...`）。
    直接 `copytree` 会把链接当实体跟随，体积白白翻倍（实测 76MB → 150MB）。
    """
    src = src.resolve()
    exe = find_python_exe(src)
    if exe is None:
        return src
    real = exe.resolve()
    try:
        parts = real.relative_to(src).parts
    except ValueError:
        return src
    return (src / parts[0]) if len(parts) > 1 else src


def bundle_portable_python(spec: dict, dst_launcher: Path, pk: str,
                           src: Path | None) -> Path | None:
    """把便携 CPython 放进 <包根>/vendor/<平台>/python（包内自带运行时）。"""
    fr = spec["frozen_runtime"]
    if not fr.get("bundle_portable_cpython"):
        return None
    if src is None:
        warn("未提供便携 CPython（--vendor-python / SKILLOS_VENDOR_PYTHON / uv 均不可用）\n"
             "      → 发行物将不自带运行时：用户机器需有 Python "
             f"{spec['python'].get('pin')}，或首次启动时由 vendor 内 uv 联网自举。")
        return None
    copy_root = _python_install_root(src)
    dst = dst_launcher / "vendor" / pk / fr.get("vendor_subdir", "python")
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(copy_root, dst)
    exe = find_python_exe(dst)
    if exe is None:
        die(f"便携 CPython 拷贝到 {dst} 后找不到解释器。")
    log(f"  ✓ 便携运行时 → {dst.relative_to(DIST)}  ({human(dir_stats(dst)['bytes'])})")
    return exe


# ---------------------------------------------------------------- 装配

def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _make_alias(exe: Path, alias: Path) -> None:
    shutil.copy2(exe, alias)
    if os.name != "nt":
        alias.chmod(0o755)


def _copy_web(spec: dict, root: Path) -> None:
    asm = spec["assemble"]
    web_src = REPO / asm["web_dist"]
    if not (web_src / "index.html").exists():
        die(f"找不到前端构建产物 {web_src}/index.html。\n"
            "      → 先在 web/ 下 `npm ci && npm run build`（这是『终端用户零 Node』的前提）。")
    _copy_tree(web_src, root / asm["web_dist_dest"])
    maps = list((root / asm["web_dist_dest"]).rglob("*.map"))
    if maps:
        die(f"前端产物里有 {len(maps)} 个 sourcemap —— 那是源码泄漏。\n"
            f"      → web/vite.config.ts 必须 build.sourcemap=false（见 docs/10 §5）。")


def _copy_skill_pack(spec: dict, root: Path) -> Path:
    """把仓库里的 skill 包（源码形态）拷成**发行包骨架**，返回包根。

    发行包里它就是**整个 skill**：`SKILL.md` / `manifest.json` / `scripts/` /
    `references/` 直接落在包根（用户装进 Agent 的 skills 目录即可被识别）；
    载荷（编译产物 / 前端 / 依赖清单）由装配的后续步骤注入 `runtime/` 下。
    """
    asm = spec["assemble"]
    src = REPO / asm["skill_src"]
    if not src.exists():
        die(f"找不到 skill 包源目录：{src}")
    pack_root = (root / (asm.get("skill_dest") or ".")).resolve()
    _copy_tree(src, pack_root)
    # 清掉构建期不该带的东西：仓库里的 vendor/runtime 是占位或上次残留，
    # start.* / BUILD-INFO.json / README.txt 一律由装配重新生成。
    for junk in ("__pycache__", ".venv", "vendor", "runtime",
                 "start.cmd", "start.sh", "BUILD-INFO.json", "README.txt"):
        p = pack_root / junk
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.is_file():
            p.unlink()
    return pack_root


def generate_launcher_manifest(src_manifest: Path, dst: Path, spec: dict,
                               pk: str, dry: bool) -> None:
    """把 skill 包的 manifest 生成为发行形态（mode=dist）。

    两条路线写出的形态不同：
      * module/frozen —— 写 `frozen` 块 + `runtimes.python.pin`；不需要 binary 块
      * standalone    —— 写 `binary` 块（roles 指向编译出的 exe）

    路径一律**相对本包根** —— 发行包里的 SKILL.md 就在上面一层，
    所以 dir 是 `runtime/skillos` 而不是早期的 `../skillos`（载荷已挪进包内）。
    """
    payload = spec["assemble"].get("payload_dest", "runtime")
    m = json.loads(src_manifest.read_text(encoding="utf-8"))
    m["mode"] = "dist"
    m["_generated_by"] = "build/build.py —— 请勿手改；改 build/nuitka.json 后重新构建"
    m["platform"] = pk

    if spec.get("route") == "module":
        mod = spec["module"]
        m.pop("binary", None)
        m["frozen"] = {
            "kind": "package-extension",
            "dir": f"{payload}/skillos",
            "requirements": f"{payload}/requirements.txt",
            "python_tag": mod.get("python_tag"),
            "python_pin": spec["python"].get("pin"),
            "_note": "skillos 已编译为扩展模块（.pyd/.so），dir 是 PYTHONPATH 注入点；"
                     "数据文件挂在 dir/<包名>/<原相对路径> 下（__file__ 在扩展模块里是"
                     "虚拟路径，必须保持这棵树）。requirements 是第三方清单。",
        }
        m.setdefault("runtimes", {})["python"] = {
            "min": spec["python"]["min"],
            "pin": spec["python"].get("pin"),
            "required": True,
            "vendor": None,
            "note": f"frozen 形态：发行物自带 CPython {spec['python'].get('pin')}"
                    "（包内 vendor/<平台>/python）；系统同小版本亦可复用。"
                    "3.12/3.13 因 ABI 不符不可用。",
        }
        if dry:
            log("[dry-run] 生成 launcher manifest (mode=dist, frozen)")
            return
        dst.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return

    shared = spec["standalone"]["shared_runtime"]["enabled"]
    if shared:
        roles = {k: {"dir": f"{payload}/skillos",
                     "name": k if k.startswith("skillos") else f"skillos-{k}"}
                 for k in ("cli", "mcp", "server", "daemon")}
        roles["cli"]["name"] = "skillos"
        roles["daemon"]["name"] = "skillos-daemon"
    else:
        roles = {
            "cli": {"dir": f"{payload}/skillos", "name": "skillos"},
            "daemon": {"dir": f"{payload}/skillos", "name": "skillos"},
            "mcp": {"dir": f"{payload}/mcp", "name": "skillos-mcp"},
            "server": {"dir": f"{payload}/server", "name": "skillos-server"},
        }
    m["binary"] = {
        "shared": shared,
        "bin_dir_rel": ".",  # roles[].dir 相对本包根（发行包里 SKILL.md 就在这一层）
        "roles": roles,
        "env": {"SKILLOS_HOST": None, "SKILLOS_PORT": None,
                "_note": "由 start.py 按 host/port 覆盖注入"},
    }
    m.setdefault("runtimes", {})["python"] = {
        "min": spec["python"]["min"],
        "required": False,
        "vendor": None,
        "note": "二进制形态：运行时已编译进产物，不需要系统 Python，也不回落 vendor",
    }
    if dry:
        log("[dry-run] 生成 launcher manifest (mode=dist, binary)")
        return
    dst.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_entry_scripts(spec: dict, root: Path, pk: str,
                        vendored_py: Path | None) -> None:
    """生成零 Python 用户也能用的入口脚本。

    为什么必须有：start.py 本身是 Python 脚本 —— 用户机器完全没有 Python 时，
    连「启动」这一步都迈不出去。这两个薄壳直接调用发行物自带的 CPython，
    缺了才回落系统 python。这是「零安装」闭环的最后一块。
    """
    names = spec["assemble"].get("entry_scripts") or []
    if not names:
        return
    rel = None
    if vendored_py is not None:
        base = root / "vendor" / pk
        try:
            rel = vendored_py.relative_to(base).as_posix()
        except ValueError:
            rel = None

    if "start.cmd" in names and pk.startswith("win"):
        py = f"%HERE%vendor\\{pk}\\{rel.replace('/', chr(92))}" if rel else "python"
        (root / "start.cmd").write_text(
            "@echo off\r\n"
            "setlocal\r\n"
            "set \"HERE=%~dp0\"\r\n"
            f"set \"PY={py}\"\r\n"
            "if not exist \"%PY%\" set \"PY=python\"\r\n"
            "\"%PY%\" \"%HERE%scripts\\start.py\" %*\r\n",
            encoding="utf-8")

    if "start.sh" in names:
        py = f"$HERE/vendor/{pk}/{rel}" if rel else "python3"
        (root / "start.sh").write_text(
            "#!/bin/sh\n"
            "# Skill OS 启动入口（零安装：优先用包内自带的 CPython）\n"
            "HERE=$(cd \"$(dirname \"$0\")\" && pwd)\n"
            f"PY=\"{py}\"\n"
            "[ -x \"$PY\" ] || PY=python3\n"
            "exec \"$PY\" \"$HERE/scripts/start.py\" \"$@\"\n",
            encoding="utf-8")
        (root / "start.sh").chmod(0o755)


def write_build_info(spec: dict, pk: str, root: Path, **extra) -> None:
    info = {
        "product": spec["product"]["name"],
        "display_name": spec["product"]["display_name"],
        "spec_version": spec["spec_version"],
        "route": spec.get("route"),
        "platform": pk,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": platform.python_version(),
        "python_pin": spec["python"].get("pin") if spec.get("route") == "module" else None,
        "nuitka": spec.get("_nuitka_version"),
        "git_commit": spec.get("_git_commit"),
        "executables": {},
        "artifacts": {},
        "dirs": {},
    }
    info.update(extra)

    payload = spec["assemble"].get("payload_dest", "runtime")
    exe_dir = root / payload / "skillos"
    if exe_dir.is_dir():
        for p in sorted(exe_dir.iterdir()):
            if p.is_file() and (p.suffix in (".exe", ".pyd", ".so")
                                or os.access(p, os.X_OK)):
                info["executables"][p.name] = {
                    "bytes": p.stat().st_size,
                    "sha256": sha256_file(p),
                }
    for rel in (f"{payload}/skillos", f"{payload}/web", "vendor", "scripts"):
        d = root / rel
        if d.is_dir():
            info["dirs"][rel] = dir_stats(d)
    for f in (f"{payload}/requirements.txt", "start.cmd", "start.sh"):
        p = root / f
        if p.is_file():
            info["artifacts"][f] = {"bytes": p.stat().st_size, "sha256": sha256_file(p)}

    # 保密自证：运行时目录里不允许出现任何 .py 源码
    runtime = root / payload / "skillos"
    leaked = [str(p.relative_to(root)) for p in runtime.rglob("*.py")] \
        if runtime.is_dir() else []
    info["source_leak_check"] = {"py_files_under_runtime": leaked,
                                 "clean": not leaked}
    (root / "BUILD-INFO.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def assemble_module(spec: dict, pk: str, pyd: Path, dry: bool,
                    vendor_py_arg: str | None,
                    out_root: Path | None = None) -> Path:
    """装配 frozen 发行包 —— 产物**本身就是一个 skill 包**：

        <root>/
        ├── SKILL.md / manifest.json / README.md / .gitignore   ← skill 的根
        ├── scripts/  references/                               ← 启动脚本与文档
        ├── start.cmd / start.sh                                ← 零安装入口
        ├── vendor/<平台>/python/                               ← 便携 CPython
        └── runtime/                                            ← 编译载荷
            ├── skillos/  （.pyd + 数据树，PYTHONPATH 注入点）
            ├── web/      （前端 SPA）
            └── requirements.txt

    整目录可任意搬移 —— 所有路径都相对包根，且 manifest 里记的也是包内相对路径。
    """
    payload = spec["assemble"].get("payload_dest", "runtime")
    root = out_root or (DIST / f"skillos-{pk}")
    if not dry:
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)

    # 1) skill 骨架：SKILL.md + manifest.json + scripts/ + references/ 落到包根
    if not dry:
        _copy_skill_pack(spec, root)
        log(f"  ✓ skill 骨架 → {root.name}/（SKILL.md + scripts/ + references/）")
    else:
        log(f"  [dry-run] skill 骨架 → {root.name}/（SKILL.md + scripts/ + references/）")

    # 2) 后端载荷：runtime/skillos/（.pyd + 数据树，<输出目录>/<包名>/ 整棵树拷过来）
    out_dir = pyd.parent if pyd else (OUT / "module")   # dry-run 时没有 pyd
    dst_runtime = root / payload / "skillos"
    if not dry:
        dst_runtime.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pyd, dst_runtime / pyd.name)
        pkg_dir = out_dir / spec["module"]["package"]
        if pkg_dir.is_dir():
            _copy_tree(pkg_dir, dst_runtime / spec["module"]["package"])
        log(f"  ✓ 后端载荷 → {payload}/skillos/{pyd.name}")
    else:
        log(f"  [dry-run] 后端载荷 → {payload}/skillos/"
            f"<{spec['module']['package']}.pyd> + 数据树")

    # 3) 第三方依赖清单 → runtime/requirements.txt
    if not dry:
        generate_requirements(spec, root / payload / spec["runtime_requirements"]["file"])

    # 4) 前端 → runtime/web/
    if not dry:
        _copy_web(spec, root)

    # 5) 便携运行时（vendor/<平台>/python）+ manifest 转 dist 形态 + 零安装入口
    if not dry:
        src = resolve_vendor_python_source(spec, vendor_py_arg)
        vendored = bundle_portable_python(spec, root, pk, src)
        mf = root / "manifest.json"
        generate_launcher_manifest(mf, mf, spec, pk, dry)
        write_entry_scripts(spec, root, pk, vendored)

    # 6) 附加文档（规格里显式列出的才随包）
    for extra in spec["assemble"].get("extra_docs", []):
        p = REPO / extra
        if p.is_file() and not dry:
            shutil.copy2(p, root / p.name)

    # 7) BUILD-INFO + 发行说明
    if not dry:
        write_build_info(spec, pk, root)
        (root / "README.txt").write_text(readme_text(spec, pk), encoding="utf-8")
    return root


def assemble_standalone(spec: dict, pk: str, built: dict[str, Path],
                        dry: bool) -> Path:
    """装配全量编译发行包（退路路线）—— 与 frozen 一样产出一个完整 skill 包。"""
    root = DIST / f"skillos-{pk}"
    asm = spec["assemble"]
    payload = asm.get("payload_dest", "runtime")
    pdir = root / payload
    shared = spec["standalone"]["shared_runtime"]["enabled"]
    if not dry:
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        _copy_skill_pack(spec, root)

    if shared:
        primary = next(t for t in standalone_targets(spec) if t.get("primary"))
        dd = built.get(primary["name"])
        if dd is None and not dry:
            die("共享运行时模式下没有拿到主构建产物。")
        if not dry:
            _copy_tree(dd, pdir / "skillos")
            exe = find_exe(pdir / "skillos", primary["output_filename"])
            for name in spec["standalone"]["shared_runtime"]["aliases"]:
                if name == primary["output_filename"]:
                    continue
                _make_alias(exe, pdir / "skillos" / f"{name}{exe.suffix}")
    else:
        for name, dd in built.items():
            t = next(x for x in standalone_targets(spec) if x["name"] == name)
            dest = "skillos" if t["output_filename"] == "skillos" else t["output_filename"]
            if not dry:
                _copy_tree(dd, pdir / dest)

    if not dry:
        _copy_web(spec, root)
        mf = root / "manifest.json"
        generate_launcher_manifest(mf, mf, spec, pk, dry)
    for extra in asm.get("extra_docs", []):
        p = REPO / extra
        if p.is_file() and not dry:
            shutil.copy2(p, root / p.name)
    if not dry:
        write_build_info(spec, pk, root)
        (root / "README.txt").write_text(readme_text(spec, pk), encoding="utf-8")
    return root


def readme_text(spec: dict, pk: str) -> str:
    pin = spec["python"].get("pin") or spec["python"]["min"]
    if spec.get("route") == "module":
        how = f"""怎么启动
  1) Windows：双击本目录的 start.cmd
     macOS / Linux：./start.sh
     脚本会优先用本包自带的 CPython {pin}，**你不需要安装任何东西**
  2) 浏览器打开输出的地址

  也可以敲命令：
     vendor/{pk}/python/python.exe scripts/start.py    ← 用包内自带运行时
     ./start.sh                                        ← 永远可用

目录说明（本目录就是一个完整 skill，可整目录搬移）
  SKILL.md          Agent 使用说明（何时启动、启动后能用哪些 MCP 工具）
  scripts/          启动器脚本（启动 / 停止 / 自检 / 环境检测）
  runtime/skillos/  产品运行时（skillos 已编译为原生扩展模块，**源码不可见**）
                    同时是 PYTHONPATH 注入点：启动时把第三方依赖装进自带 venv，
                    再从这里加载 skillos
  runtime/requirements.txt  第三方依赖清单（首次启动时自动安装，走镜像加速）
  runtime/web/      前端预构建产物（你不需要 Node）
  vendor/{pk}/python/  包内自带的 CPython 运行时
  BUILD-INFO.json   版本、校验和、以及「源码泄漏自检」结果

需要什么
  * 网络：仅首次启动装第三方依赖时需要（可用 `--offline` 跳过，但需预置 venv）
  * git：Posix 自带；Windows 可放便携 MinGit 到 vendor/{pk}/ 下
  * Python：**不需要**（本包自带 {pin}）
  * Node：**不需要**

安全与数据
  * 凭据（token / JWT secret）只落在用户主目录 ~/.skillos/，不进本目录
  * 后端不存储 Skill 文件内容；真值始终在本地 git 仓库与磁盘
  * 源码保护边界：runtime/skillos/ 是机器码；scripts/*.py 是明文薄壳
    （只做编排，不含业务逻辑）
"""
    else:
        how = f"""怎么启动
  1) 进入 scripts/
  2) python start.py            ← 需要本机 Python ≥ {spec['python']['min']}（只用来跑启动脚本）
  3) 浏览器打开输出的地址

  说明：启动脚本本身是明文 Python（薄薄一层，只做编排）；真正的产品逻辑
  全部在 runtime/skillos/ 的二进制里。若客户机器完全没有 Python，可直接跑
  runtime/skillos/ 下的 skillos-server（见 BUILD-INFO.json 的 executables）。

目录说明（本目录就是一个完整 skill，可整目录搬移）
  SKILL.md          Agent 使用说明（何时启动、启动后能用哪些 MCP 工具）
  scripts/          启动器脚本（启动 / 停止 / 自检 / 环境检测）
  runtime/          运行时与可执行文件（源码不可见：已编译为原生二进制）
                      skillos          命令行（含 `skillos daemon`）
                      skillos-mcp      MCP stdio 服务（供 Claude Code / Codex 等接入）
                      skillos-server   后端 + Web 控制台（单端口托管 SPA）
                      skillos-daemon   等价 `skillos daemon`
  runtime/web/      前端预构建产物（终端用户不需要 Node）
  BUILD-INFO.json   版本与校验和

需要什么
  * git：POSIX 系统自带；Windows 请把便携版 MinGit 放到 vendor/{pk}/（启动器会自动识别）
  * Python：仅启动脚本需要（≥ {spec['python']['min']}）；直接跑二进制则不需要
  * Node：**不需要**

安全与数据
  * 凭据（token / JWT secret）只落在用户主目录 ~/.skillos/，不进本目录
  * 后端不存储 Skill 文件内容；真值始终在本地 git 仓库与磁盘
"""
    return f"""{spec['product']['display_name']} —— 发行包（{pk}）

编译路线：{spec.get('route')}

{how}"""


# ---------------------------------------------------------------- 主流程

def git_commit() -> str | None:
    try:
        p = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           cwd=str(REPO), capture_output=True, text=True, timeout=20)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def print_summary(root: Path, payload: str = "runtime") -> None:
    total = dir_stats(root)
    log("")
    log("=== skill 包装配完成 ===")
    log(f"  交付目录  {root}   ← 整目录就是那个 skill 包，压缩即交付")
    log(f"  体积      {human(total['bytes'])} / {total['files']} 文件")
    for p in sorted(root.iterdir()):
        if p.is_file():
            log(f"    {p.name:<20} {human(p.stat().st_size)}")
        else:
            s = dir_stats(p)
            log(f"    {p.name + '/':<20} {human(s['bytes'])} ({s['files']} 文件)")
    exe_dir = root / payload / "skillos"
    if exe_dir.is_dir():
        log("  运行时：")
        for p in sorted(exe_dir.iterdir()):
            if p.is_file() and (p.suffix in (".exe", ".pyd", ".so") or os.access(p, os.X_OK)):
                log(f"    {p.name:<28} {human(p.stat().st_size)}  {sha256_file(p)[:16]}")
    info = json.loads((root / "BUILD-INFO.json").read_text(encoding="utf-8"))
    leak = info.get("source_leak_check") or {}
    log(f"  源码泄漏自检：{'通过（运行时目录无 .py）' if leak.get('clean') else '⚠ 有 .py！'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Skill OS 编译发行驱动（Nuitka）")
    ap.add_argument("--list", action="store_true", help="列出路线与规格后退出")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令，不执行")
    ap.add_argument("--route", choices=["module", "standalone"], default=None,
                    help="覆盖 build/nuitka.json 的 route")
    ap.add_argument("--target", action="append", default=None,
                    help="（仅 standalone）只构建指定 target（可重复）")
    ap.add_argument("--skip-build", action="store_true", help="复用 build/out，只重新装配")
    ap.add_argument("--clean", action="store_true", help="先清 build/out 与 build/dist")
    ap.add_argument("--compiler", choices=["auto", "mingw64", "msvc"], default=None,
                    help="Windows 编译器选择（默认取规格）")
    ap.add_argument("--vendor-python", default=None,
                    help="要随包的便携 CPython 目录（含 python.exe 或其一级子目录）")
    ap.add_argument("--out-dir", default=None,
                    help="把装配好的 skill 包输出到该目录"
                         "（默认 build/dist/skillos-<平台>）")
    ap.add_argument("--version-string", default=None, help="覆盖产物版本号（默认 0.1.0）")
    ap.add_argument("--no-archive", action="store_true", help="不生成 zip 发行包")
    args = ap.parse_args()

    spec = load_spec()
    pk = platform_key()
    if args.route:
        spec["route"] = args.route

    if args.list:
        log(f"平台：{pk}")
        log(f"路线：{spec.get('route')}")
        if spec.get("route") == "module":
            mod = spec["module"]
            log(f"  编译模式 : --mode={mod['nuitka_mode']} {mod['package']}"
                f"（第三方按 site-packages 自动排除）")
            log(f"  ABI 锁   : Python {spec['python'].get('pin')} → {mod.get('python_tag')}")
            log(f"  便携运行时: "
                f"{'随包' if spec['frozen_runtime'].get('bundle_portable_cpython') else '不随包'}")
        else:
            shared = spec["standalone"]["shared_runtime"]["enabled"]
            log(f"  共享运行时：{shared}")
            for t in standalone_targets(spec):
                tag = " [primary]" if t.get("primary") else (
                    " [fallback]" if t.get("fallback_only") else "")
                log(f"  - {t['name']}{tag}\n"
                    f"      output: {t['output_filename']}\n"
                    f"      entry : {t['entry']}")
        return

    spec["_product_version"] = normalize_version(args.version_string or os.environ.get("SKILLOS_VERSION"))
    spec["_git_commit"] = git_commit()
    if args.compiler:
        spec["_compiler"] = args.compiler

    if args.clean and not args.dry_run:
        for d in (OUT, DIST):
            if d.exists():
                log(f"清理 {d}")
                shutil.rmtree(d, ignore_errors=True)

    log(f"=== Skill OS 构建 · 平台 {pk} · Python {platform.python_version()} "
        f"· 路线 {spec.get('route')} ===")
    check_python(spec)
    check_entrypoints(spec)
    check_data_files(spec)
    spec["_resolved_compiler"] = check_compiler(spec, pk, args.dry_run)
    if pk.startswith("win"):
        log(f"  编译器：{spec['_resolved_compiler']}")
    spec["_nuitka_version"] = check_nuitka(spec, args.dry_run)
    if not args.dry_run:
        OUT.mkdir(parents=True, exist_ok=True)

    if spec.get("route") == "module":
        module_out = OUT / "module"
        pyd: Path | None = None
        if args.skip_build:
            cands = sorted(list(module_out.glob("*.pyd")) + list(module_out.glob("*.so")))
            if not cands:
                die("--skip-build 但 build/out/module 下找不到 .pyd/.so。先跑一次完整构建。")
            pyd = cands[-1]
            log(f"\n--- 复用已有产物：{pyd.name} ---")
            copy_data_files(spec, module_out)
        else:
            log(f"\n--- 构建扩展模块：{spec['module']['package']} ---")
            pyd = build_module(spec, pk, args.dry_run)
        log("\n--- 装配 skill 包 ---")
        out_root = Path(args.out_dir).expanduser().resolve() if args.out_dir else None
        root = assemble_module(spec, pk, pyd, args.dry_run, args.vendor_python, out_root)
    else:
        shared = spec["standalone"]["shared_runtime"]["enabled"]
        tgts = standalone_targets(spec)
        if shared:
            wanted = [t for t in tgts if t.get("primary")]
            if args.target:
                wanted = [t for t in tgts if t["name"] in args.target] or wanted
        else:
            wanted = [t for t in tgts if not t.get("fallback_only")]
            if args.target:
                wanted = [t for t in wanted if t["name"] in args.target]

        built: dict[str, Path] = {}
        if not args.skip_build:
            for t in wanted:
                log(f"\n--- 构建 target: {t['name']} ---")
                dd = build_target(spec, t, OUT, pk, args.dry_run)
                if dd:
                    built[t["name"]] = dd
                    log(f"  ✓ {dd.relative_to(REPO)}")
        else:
            for t in wanted:
                for cand in (OUT / f"{t['name']}.dist",
                             OUT / f"{Path(t['entry']).stem}.dist"):
                    if cand.is_dir():
                        built[t["name"]] = cand
                        break
            if not built:
                die("--skip-build 但 build/out 下找不到任何 *.dist。先跑一次完整构建。")
        log("\n--- 装配 skill 包 ---")
        root = assemble_standalone(spec, pk, built, args.dry_run)

    if args.dry_run:
        log("\n[dry-run] 未产生任何产物。命令均已打印，可交由 CI 或在有编译器的机器上执行。")
        return

    print_summary(root, spec["assemble"].get("payload_dest", "runtime"))

    if not args.no_archive:
        base = root.parent / f"skillos-{spec['_product_version']}-{pk}"
        log("\n--- 打包 ---")
        try:
            zpath = shutil.make_archive(str(base), "zip", root_dir=root.parent,
                                        base_dir=root.name)
            log(f"  ✓ {zpath}  {human(Path(zpath).stat().st_size)}")
        except OSError as e:
            log(f"  ! 打包失败（产物目录本身可用）：{e}")
            log(f"      → Windows 上若提示路径过长，可先 `--no-archive`，"
                f"改在有长路径支持的机器上打包。")

    log("\n下一步：python build/smoke.py --dist <上面的目录>  （对产物做四重自证）")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        print(f"build: 未预期错误：{e}", file=sys.stderr, flush=True)
        sys.exit(1)
