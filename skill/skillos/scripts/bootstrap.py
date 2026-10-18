"""依赖引导 —— 幂等。跑一次装好，重复跑不重复装。

三种发行形态（自动识别，无需手工切换）：

* **repo**   —— 源码仓库形态。建仓内 venv，`pip install -e .` 装 skillos 本身。
* **frozen** —— D1 编译发行形态：`skillos` 是编译好的扩展模块（.pyd/.so），
                第三方交给 venv。所以这里**只装 requirements.txt，不装 skillos**，
                运行期用 PYTHONPATH 指到编译产物所在目录。
* **binary** —— 全量编译形态：连解释器都编进产物，什么都不用装。

顺序（**检测优先**）：
    1. 探测运行时；系统 Python 达标 → 用它做 venv 基底；否则回落 vendor
       （frozen 形态下"达标"= 与发行物同一小版本，见 manifest.runtimes.python.pin）
    2. 建仓内 venv（`<state_dir>/.venv`）—— 依赖装这里，不污染系统 Python
    3. 装依赖（repo：`pip install -e .`；frozen：`pip install -r requirements.txt`）
    4. 校验 git 可用（缺则报错给指引）
    5. 非 dev 模式下确保有 SPA 构建产物：先看现成的 dist，再解包内归档
       `vendor/dist/web-dist.zip`（离线可用、不需要 node），最后才尝试联网构建

用法：
    python scripts/bootstrap.py              # 正常装
    python scripts/bootstrap.py --dev        # 同时要求 node，用于前端热更
    python scripts/bootstrap.py --offline    # 禁一切外呼（离线机器）
    python scripts/bootstrap.py --force      # 即使已就绪也重装依赖
    python scripts/bootstrap.py --mirror cn  # 国内镜像（pip 清华 + npm npmmirror）

包源优先级：`--mirror`/`--pip-index` > 环境变量（`SKILLOS_PIP_INDEX` / `PIP_INDEX_URL`）
> `manifest.json` 的 `mirrors` 块 > 官方源。国内网络直连 PyPI 常慢到不可用，
建议国内用户默认带 `--mirror cn`。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402

# 国内镜像预设。`--mirror cn` 一次设好 pip + npm；也可传具体 URL。
# 为什么内置：本项目面向国内交付，直连 PyPI / npm 经常慢到不可用
# （实测本机直连 ~50KB/s，清华源可到 ~1MB/s 量级）。
_MIRROR_PRESETS: dict[str, dict[str, str]] = {
    "cn": {
        "pip_index": "https://pypi.tuna.tsinghua.edu.cn/simple",
        "npm_registry": "https://registry.npmmirror.com",
    },
    "tuna": {"pip_index": "https://pypi.tuna.tsinghua.edu.cn/simple"},
    "aliyun": {"pip_index": "https://mirrors.aliyun.com/pypi/simple/"},
    "tencent": {"pip_index": "https://mirrors.cloud.tencent.com/pypi/simple/"},
    "ustc": {"pip_index": "https://pypi.mirrors.ustc.edu.cn/simple/"},
}


def resolve_mirrors(m: dict, mirror: str | None, pip_index: str | None,
                    npm_registry: str | None) -> dict:
    """决定用哪个包源。优先级：CLI 显式 > 环境变量 > manifest.mirrors > 官方。

    环境变量：SKILLOS_PIP_INDEX / PIP_INDEX_URL / SKILLOS_NPM_REGISTRY / npm_config_registry
    """
    cfg = m.get("mirrors") or {}
    pip_i = (pip_index
             or os.environ.get("SKILLOS_PIP_INDEX")
             or os.environ.get("PIP_INDEX_URL")
             or cfg.get("pip_index"))
    npm_r = (npm_registry
             or os.environ.get("SKILLOS_NPM_REGISTRY")
             or os.environ.get("npm_config_registry")
             or cfg.get("npm_registry"))
    src = "default"
    if mirror:
        key = mirror.strip().lower()
        if key in ("none", "off", "official"):
            return {"pip_index": None, "npm_registry": None, "source": "--mirror none"}
        preset = _MIRROR_PRESETS.get(key)
        if preset is None:
            if not mirror.startswith(("http://", "https://")):
                C.die(f"未知的 --mirror 预设：{mirror}\n"
                      f"      → 可用预设：{', '.join(sorted(_MIRROR_PRESETS))}\n"
                      "      → 或直接给 URL（将作为 pip index 使用）")
            pip_i = mirror
        else:
            pip_i = preset.get("pip_index", pip_i)
            npm_r = preset.get("npm_registry", npm_r)
        src = f"--mirror {key}"
    elif pip_i is not None:
        src = "env/manifest"
    return {"pip_index": pip_i, "npm_registry": npm_r, "source": src}


def mirror_env(offline: bool, mirrors: dict) -> dict:
    """把包源注入环境变量（pip / uv / npm 都会读）。"""
    env = os.environ.copy()
    if offline:
        env["PIP_NO_INDEX"] = "1"
        return env
    if mirrors.get("pip_index"):
        env["PIP_INDEX_URL"] = mirrors["pip_index"]
        env["UV_INDEX_URL"] = mirrors["pip_index"]      # uv 认这个
    if mirrors.get("npm_registry"):
        env["npm_config_registry"] = mirrors["npm_registry"]
    return env


def find_managed_python(root: Path) -> Path | None:
    """托管/便携 CPython 的定位逻辑与 _common 共用一套 glob（避免两处漂移）。"""
    return C.find_python_in(root)


def _run(argv: list[str], cwd: Path | None = None, env: dict | None = None,
         what: str = "") -> int:
    C.log(f"  $ {' '.join(str(a) for a in argv)}")
    try:
        p = subprocess.run([str(a) for a in argv], cwd=str(cwd) if cwd else None,
                           env=env)
        return p.returncode
    except FileNotFoundError:
        C.die(f"{what or '命令'} 不可执行：{argv[0]} 不存在")
    except OSError as e:
        C.die(f"{what or '命令'} 执行失败：{e}")


def ensure_uv_python(uv: Path, install_dir: Path, version: str) -> None:
    env = os.environ.copy()
    env["UV_PYTHON_INSTALL_DIR"] = str(install_dir)
    C.log(f"[1/5] 用 vendor 内 uv 自举 Python {version} → {install_dir}")
    rc = _run([uv, "python", "install", version, "--install-dir", str(install_dir)],
              env=env, what="uv python install")
    if rc != 0:
        C.die(f"uv 自举 Python 失败（退出码 {rc}）。离线机器请先在 vendor 预置 CPython。")


def resolve_base_python(rt: dict, m: dict, offline: bool) -> tuple[list[str], str, str | None]:
    """返回 (基础解释器 argv, 来源标签, 版本)。检测优先。"""
    py = rt["python"]
    src = py.get("source")

    if src == "system":
        return list(py["argv"]), "system", py.get("version")

    # vendor 路径：uv 在包内
    if src in ("vendor", "vendor-uv-online"):
        pydir = Path(py["python_dir"])
        exe = find_managed_python(pydir)
        if exe:
            return [str(exe)], "vendor(cpython)", py.get("version")
        if offline:
            C.die("离线模式且 vendor 内无预置 CPython，同时系统无可用 Python。\n"
                  "      → 在有网机器执行 scripts/vendor_fetch.py 后整体拷贝 vendor/ 过来。")
        ensure_uv_python(Path(py["uv"]), pydir, str(m.get("runtimes", {})
                                                    .get("python", {})
                                                    .get("version") or "3.13"))
        exe = find_managed_python(pydir)
        if exe:
            return [str(exe)], "vendor(uv)", f"managed@{pydir.name}"
        C.die(f"uv 已执行但未在 {pydir} 找到 Python 解释器。")

    C.die("未发现可用 Python（系统无 ≥3.11 版本，且 vendor 内无 uv）。\n"
          "      → 安装 Python 3.11+ 后重试，或放入 vendor/<平台>/uv。")


def ensure_venv(base: list[str], venv: Path) -> Path:
    vpy = C.venv_python(venv)
    if vpy.exists():
        C.log(f"[2/5] venv 已存在，复用：{venv}")
        return vpy
    C.log(f"[2/5] 创建仓内 venv：{venv}")
    rc = _run([*base, "-m", "venv", str(venv)], what="python -m venv")
    if rc != 0 or not vpy.exists():
        C.die("创建 venv 失败。Debian/Ubuntu 上可能需先 `apt install python3-venv`。")
    return vpy


def ensure_pip(vpy: Path) -> None:
    p = subprocess.run([str(vpy), "-m", "pip", "--version"],
                       capture_output=True, text=True)
    if p.returncode == 0:
        return
    C.log("  venv 内无 pip，尝试 ensurepip …")
    _run([str(vpy), "-m", "ensurepip", "--upgrade"], what="ensurepip")


def skillos_ready(vpy: Path) -> bool:
    # `-P`（isolated path，Python 3.11+）不可省：它不再把当前 cwd 塞进 sys.path。
    # 否则从仓库根运行 `python scripts/bootstrap.py` 时，`import skillos`
    # 会命中仓库里的 `skillos/` 源码目录 → 假阳性 → 明明没装却判定"已就绪"。
    #
    # **不能用 `-I`**：`-I` 隐含 `-E`，会把 PYTHONPATH 一并忽略 —— 而 frozen
    # 形态的 skillos 恰恰只能靠 PYTHONPATH 找到（它不装在 venv 里）。
    p = subprocess.run([str(vpy), "-P", "-c", "import skillos"],
                       capture_output=True, text=True)
    return p.returncode == 0


def deps_ready(vpy: Path, m: dict) -> bool:
    """frozen 形态的就绪判据：**第三方 + 编译产物都能 import** 才算好。

    刻意抽几个覆盖不同加载方式的重依赖（原生扩展 / 惰性 import），
    比只看 `import skillos` 更能说明问题。
    """
    env = os.environ.copy()
    pp = C.frozen_pythonpath(m)
    if pp:
        env["PYTHONPATH"] = pp
    code = "import skillos, fastapi, sqlalchemy, uvicorn, mcp, pydantic"
    p = subprocess.run([str(vpy), "-P", "-c", code],
                       capture_output=True, text=True, env=env)
    return p.returncode == 0


def install_requirements(vpy: Path, m: dict, offline: bool, force: bool,
                         mirrors: dict | None = None) -> None:
    """frozen 形态：只装第三方清单，**不装 skillos**（它由编译产物提供）。"""
    req = C.frozen_requirements(m)
    if req is None or not req.is_file():
        C.die("这是 frozen（编译发行）形态，但找不到依赖清单 requirements.txt。\n"
              "      → 发行包不完整，请重新构建或重新解压。")
    if deps_ready(vpy, m) and not force:
        C.log(f"[3/5] 第三方依赖已就绪（{req.name}；--force 可强制重装）")
        return
    mirrors = mirrors or {}
    env = mirror_env(offline, mirrors)
    idx = (["--index-url", mirrors["pip_index"]]
           if mirrors.get("pip_index") and not offline else [])
    C.log(f"[3/5] 安装第三方依赖：pip install -r {req.name}（{len(_req_lines(req))} 个）")
    rc = _run([str(vpy), "-m", "pip", "install", "--disable-pip-version-check",
               *idx, "-r", str(req)], env=env, what="pip install -r")
    if rc != 0:
        C.die(f"安装依赖失败（退出码 {rc}）。\n"
              "      → 国内网络可加 `--mirror cn`；离线机器请预置 venv 后整体拷贝。")


def _req_lines(req: Path) -> list[str]:
    try:
        return [x for x in req.read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return []


def install_skillos(vpy: Path, mode: str, repo: Path | None, m: dict,
                    offline: bool, force: bool, mirrors: dict | None = None) -> None:
    if skillos_ready(vpy) and not force:
        C.log("[3/5] skillos 已在 venv 内就绪（--force 可强制重装）")
        return
    mirrors = mirrors or {}
    env = mirror_env(offline, mirrors)
    idx = (["--index-url", mirrors["pip_index"]]
           if mirrors.get("pip_index") and not offline else [])

    if mode == "repo":
        if not repo:
            C.die("repo 模式但找不到仓库根（需 pyproject.toml + skillos/）。")
        C.log(f"[3/5] 可编辑安装：pip install -e {repo}")
        rc = _run([str(vpy), "-m", "pip", "install", *idx, "-e", str(repo)],
                  cwd=repo, env=env, what="pip install -e")
    else:
        wheel_dir = C.vendor_dir() / "wheel"
        if wheel_dir.is_dir() and any(wheel_dir.glob("*.whl")):
            C.log(f"[3/5] 离线安装内置 wheel：{wheel_dir}")
            rc = _run([str(vpy), "-m", "pip", "install", "--no-index",
                       "--find-links", str(wheel_dir), "skillos"], env=env,
                      what="pip install wheel")
        else:
            ver = m.get("skillos_version")
            spec = f"skillos=={ver}" if ver else "skillos"
            C.log(f"[3/5] 安装 {spec}（PyPI）")
            rc = _run([str(vpy), "-m", "pip", "install", *idx, spec], env=env,
                      what="pip install")
    if rc != 0:
        C.die(f"安装 skillos 失败（退出码 {rc}）。")


def ensure_git(rt: dict) -> None:
    g = rt["git"]
    if g.get("source") == "missing":
        C.die("未发现 git。请安装 git（Windows 可放 vendor/<平台>/MinGit 兜底）。")
    C.log(f"[4/5] git 就绪：{g.get('source')} {g.get('version') or g.get('path')}")


def web_archive() -> Path:
    """包内归档的前端产物 —— 由 skillos-builder 从 CI 的 `web-dist` artifact 原样拉回。

    `vendor/dist/` 是归档区（gitignored、只留最新版本），文件名固定 `web-dist.zip`，
    zip 根就是 `index.html` + `assets/`。
    """
    return C.package_root() / "vendor" / "dist" / "web-dist.zip"


def extract_web_archive() -> Path | None:
    """把包内 `vendor/dist/web-dist.zip` 解到 `<包>/web_dist`；成功返回该目录。

    为什么需要这一步：交付归档里本来就带着前端，离线机 / 无 node 的机器也能单端口
    起 Web；但此前没有任何代码读它 —— 用户手里有产物，却被告知"单端口不可用"。
    解压是纯本地操作，所以排在联网构建**之前**（离线优先）。

    失败（无 zip / 坏 zip / 解包后没有 index.html）只记日志并返回 None，
    由调用方继续走 node 构建或 --dev —— 前端缺位不该阻断依赖安装。
    """
    archive = web_archive()
    if not archive.is_file():
        return None
    dest = C.package_root() / "web_dist"
    C.log(f"[5/5] 解包前端产物：{archive.name} → {dest}")
    try:
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as z:
            # 来源是本包 vendor/ 下的自有归档（非外部输入）；extractall 自身会
            # 剥掉绝对路径与 `..` 分量，不存在路径穿越。
            z.extractall(dest)
    except (OSError, zipfile.BadZipFile) as e:
        C.log(f"  解包失败：{e}（继续尝试其它方式）")
        return None
    if not (dest / "index.html").is_file():
        C.log("  解包后没有 index.html —— 归档不完整，已忽略")
        return None
    return dest


def ensure_web_dist(rt: dict, repo: Path | None, dev: bool, offline: bool,
                    mirrors: dict | None = None) -> None:
    if dev:
        C.log("[5/5] dev 模式：Web 走 vite 热更，跳过 dist 检查")
        return
    dist = C.web_dist_dir(repo)
    if dist:
        C.log(f"[5/5] SPA 构建产物已就绪：{dist}")
        return
    if extract_web_archive():
        C.log(f"[5/5] SPA 已就绪（包内归档）：{C.web_dist_dir(repo)}")
        return
    if offline:
        C.log("[5/5] 离线且无 web/dist、包内也没有 vendor/dist/web-dist.zip —— 单端口不可用")
        C.log("      → 可用 --dev 走 vite；或把 CI 的 web-dist.zip 放到 vendor/dist/ 后重跑")
        return
    src = C.web_src_dir(repo)
    node = rt["node"] if rt["node"].get("source") == "system" else None
    if src and node:
        C.log(f"[5/5] 构建 SPA：cd {src} && npm ci && npm run build")
        env = mirror_env(offline, mirrors or {})
        if _run(C.npm_argv(["ci"]), cwd=src, env=env, what="npm ci") != 0:
            C.log("  npm ci 失败，尝试 npm install …")
            _run(C.npm_argv(["install"]), cwd=src, env=env, what="npm install")
        if _run(C.npm_argv(["run", "build"]), cwd=src, env=env,
                what="npm run build") != 0:
            C.log("  构建失败 —— 单端口模式不可用，可改用 --dev。")
            return
        if C.web_dist_dir(repo):
            C.log("  构建完成。")
            return
    C.log("[5/5] 无 web/dist 且无法构建（缺 node 或前端源码）—— 单端口不可用")
    C.log("      → 有 node：cd web && npm ci && npm run build")
    C.log("      → 无 node：把 CI 的 web-dist.zip 放进 vendor/dist/，"
          "或手动解到 web_dist/（zip 根即 index.html）")
    C.log("      → 或先用 --dev 走 vite 热更")


def main() -> None:
    ap = argparse.ArgumentParser(description="幂等安装 SkillOS 运行依赖")
    ap.add_argument("--dev", action="store_true", help="dev 模式（把 node 纳入必需）")
    ap.add_argument("--offline", action="store_true", help="禁一切外呼")
    ap.add_argument("--force", action="store_true", help="即使已就绪也重装依赖")
    ap.add_argument("--mirror", default=None,
                    help="包源预设：cn / tuna / aliyun / tencent / ustc，"
                         "或直接给 URL；none = 官方源")
    ap.add_argument("--pip-index", default=None, help="自定义 pip index URL")
    ap.add_argument("--npm-registry", default=None, help="自定义 npm registry")
    args = ap.parse_args()

    C.setup_stdio()
    m = C.load_manifest()
    mode, repo = C.resolve_mode(m)
    frozen = C.is_frozen_mode(m)

    mirrors = resolve_mirrors(m, args.mirror, args.pip_index, args.npm_registry)
    if not args.offline and (mirrors.get("pip_index") or mirrors.get("npm_registry")):
        C.log(f"包源：pip = {mirrors.get('pip_index') or '官方 PyPI'}")
        C.log(f"      npm = {mirrors.get('npm_registry') or '官方 npm'}")
        C.log(f"      （来源：{mirrors.get('source')}；直连慢时可加 `--mirror cn`）")

    if C.is_binary_mode(m):
        # 编译发行形态：运行时（含解释器）已编进 skillos/ 的二进制里。
        # 此时建 venv / 装依赖是纯粹的浪费，且会让用户以为「还要装 Python」。
        roles = C.binary_roles(m)
        C.log(f"=== SkillOS bootstrap · {C.platform_key()} · 二进制形态 ===")
        C.log("无需安装任何依赖：运行时已编译进发行物。")
        for role, info in roles.items():
            mark = "✓" if info["available"] else "✗"
            C.log(f"  {mark} {role:<7} {info['path']}")
        C.log("")
        C.log("下一步：python scripts/detect.py 看环境，或 python scripts/start.py 直接启动。")
        # git 仍然是硬依赖（真值在本地 git 仓库）
        rt = C.resolve_runtimes(m, dev_mode=args.dev)
        if rt["git"].get("source") == "missing":
            C.log("")
            C.log("⚠ 未发现可用 git —— 它是硬依赖（canonical 真值在本地 git 仓库）。")
            C.log("  Windows 可把便携版 MinGit 放到 launcher 同级的 MinGit/ 目录。")
            sys.exit(1)
        return

    C.log(f"=== SkillOS bootstrap · {C.platform_key()} · {mode} 模式"
          f"{'（frozen 编译发行）' if frozen else ''} ===")

    C.state_dir(m)
    secret, created = C.ensure_secret(m)
    C.log(f"JWT secret: {'新生成' if created else '复用'} → {secret}")

    rt = C.resolve_runtimes(m, dev_mode=args.dev)
    base, src, ver = resolve_base_python(rt, m, args.offline)
    C.log(f"[1/5] 基础 Python: {src} {ver or ''}  ({' '.join(base)})")
    if frozen:
        pin = (m.get("runtimes", {}).get("python") or {}).get("pin")
        if pin:
            C.log(f"      ABI 锁定：发行物由 CPython {pin} 编译，运行机必须是同一小版本")

    venv = C.venv_dir(m)
    vpy = ensure_venv(base, venv)
    ensure_pip(vpy)
    if frozen:
        install_requirements(vpy, m, args.offline, args.force, mirrors)
    else:
        install_skillos(vpy, mode, repo, m, args.offline, args.force, mirrors)
    ensure_git(rt)
    ensure_web_dist(rt, repo, args.dev, args.offline, mirrors)

    # 记录本次解析结果（status.py 据此展示「系统 / vendor」）
    state = C.read_state(m)
    state["launcher_version"] = m.get("launcher_version")
    state["mode"] = mode
    state["frozen"] = frozen
    state["platform"] = rt["platform"]
    state["runtimes"] = {"python": rt["python"], "git": rt["git"], "node": rt["node"]}
    state["venv"] = str(venv)
    state["bootstrap"] = {"python_source": src, "updated_at": C.now_iso(),
                          "mirrors": {"pip_index": mirrors.get("pip_index"),
                                      "npm_registry": mirrors.get("npm_registry")},
                          "requirements": (str(C.frozen_requirements(m))
                                           if frozen else None)}
    C.write_state(state, m)

    C.log("")
    C.log(f"完成。venv: {vpy}")
    C.log("下一步：python scripts/start.py")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"bootstrap: {e}", file=sys.stderr)
        sys.exit(1)
