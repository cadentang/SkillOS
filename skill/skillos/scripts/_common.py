"""SkillOS Launcher 公共库 —— **只用 Python 标准库**。

为什么必须是纯标准库：这套脚本要在「依赖还没装好」的那一刻就能跑。
一旦 import 了第三方包（哪怕是 skillos 自己），在裸机上第一步就崩了。

职责边界（各脚本共用，不重复实现）：
    - 路径与状态落位（state.json / secret / logs）
    - 平台键（win-x64 / mac-arm64 / ...）
    - 版本探测与比较、HTTP 与健康自检、端口探测
    - PID 存活判断与跨平台优雅终止、detached 拉起（日志重定向）
    - 运行时解析（**检测优先**：系统可达且达标 → 用系统；否则回落 vendor）
    - 本地配对（register(409 幂等) → login → /api/pairing/code）

安全约定：token / secret / PID 一律只落 `~/.skillos/`（或 SKILLOS_LAUNCHER_STATE），
绝不写进项目目录（NFR-2）。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows 常量（仅 os.name == "nt" 时使用）
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


# ---------------------------------------------------------------- 基础路径

def package_root() -> Path:
    """skillos skill 包根目录（scripts/ 的上一级）。

    对外发行时这个根目录里就是一个完整 skill：SKILL.md / scripts/ / references/ /
    vendor/<平台>/（便携 CPython）/ runtime/（编译载荷）。整目录可任意搬移 —— 所有
    相对路径都以本函数为基准，不看 CWD。
    """
    return Path(__file__).resolve().parent.parent


def manifest_path() -> Path:
    return package_root() / "manifest.json"


def load_manifest() -> dict:
    p = manifest_path()
    if not p.exists():
        die(f"找不到发行清单 {p}；launcher 包不完整。")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        die(f"发行清单解析失败 {p}: {e}")


def expand(p: str | os.PathLike) -> Path:
    """展开 ~ 与 $VAR，返回绝对路径。"""
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def state_dir(manifest: dict | None = None) -> Path:
    m = manifest or load_manifest()
    raw = os.environ.get("SKILLOS_LAUNCHER_STATE") or m["paths"]["state_dir"]
    d = expand(raw)
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir(manifest: dict | None = None) -> Path:
    d = state_dir(manifest) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_file(manifest: dict | None = None) -> Path:
    m = manifest or load_manifest()
    return state_dir(m) / m["paths"].get("state_file", "state.json")


def vendor_dir() -> Path:
    return package_root() / "vendor"


# ---------------------------------------------------------------- JSON 读写

def read_json(path: str | os.PathLike, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: str | os.PathLike, obj) -> None:
    """原子写（先写临时文件再 replace），避免半截文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def read_state(manifest: dict | None = None) -> dict:
    return read_json(state_file(manifest), {}) or {}


def write_state(data: dict, manifest: dict | None = None) -> None:
    write_json(state_file(manifest), data)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ---------------------------------------------------------------- 平台与版本

def platform_key() -> str:
    """归一化成 vendor/ 目录用的平台键。"""
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    if sysname == "windows":
        osname = "win"
    elif sysname == "darwin":
        osname = "mac"
    else:
        osname = "linux"
    if machine in ("amd64", "x86_64", "x64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("x86", "i386", "i686"):
        arch = "x86"
    else:
        arch = machine or "unknown"
    return f"{osname}-{arch}"


_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(text):
    """从任意版本串里抽出 (major, minor, patch)；抽不到返回 None。"""
    if not text:
        return None
    m = _VERSION_RE.search(str(text))
    if not m:
        return None
    return tuple(int(x) for x in m.groups(default="0"))


def _pad(v):
    return tuple(v) + (0,) * (3 - len(v))


def version_ge(actual, minimum) -> bool:
    a = parse_version(actual) if isinstance(actual, (str, bytes)) else actual
    b = parse_version(minimum) if isinstance(minimum, (str, bytes)) else minimum
    if a is None:
        return False
    if b is None:
        return True
    return _pad(a) >= _pad(b)


def which(cmd: str) -> str | None:
    return shutil.which(cmd)


def probe_version(cmd: list[str], timeout: int = 10) -> tuple[bool, str | None]:
    """跑 `<cmd> --version` 类命令，成功时返回首行。失败不抛异常。"""
    try:
        p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False, None
    if p.returncode != 0:
        return False, None
    out = (p.stdout or p.stderr or "").strip().splitlines()
    return True, (out[0].strip() if out else "")


# ---------------------------------------------------------------- HTTP / 健康

def http_json(base: str, method: str, path: str, body=None, token: str | None = None,
              timeout: float = 10.0) -> dict:
    url = base.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def health(base: str, timeout: float = 3.0) -> dict | None:
    """返回 /api/health 的 data（不通返回 None）。"""
    try:
        d = http_json(base, "GET", "/api/health", timeout=timeout)
    except Exception:  # noqa: BLE001 —— 探测类调用，任何异常都视为「不通」
        return None
    if not d.get("ok"):
        return None
    return d.get("data") or {}


def health_ok(base: str, timeout: float = 3.0) -> bool:
    data = health(base, timeout=timeout)
    return bool(data) and data.get("status") == "up"


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, int(port))) == 0


def wait_health(base: str, seconds: float = 40.0, interval: float = 0.6) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if health_ok(base, timeout=2.0):
            return True
        time.sleep(interval)
    return health_ok(base, timeout=2.0)


# ---------------------------------------------------------------- PID / 进程

def pid_alive(pid) -> bool:
    """跨平台判断 PID 是否存活。PID 可能被复用，这里只用于「本会话拉起的进程」。"""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    WAIT_TIMEOUT = 0x00000102
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(
        SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
    )
    if not handle:
        return False
    try:
        return k32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        k32.CloseHandle(handle)


def spawn_detached(cmd: list, cwd=None, env: dict | None = None,
                   log_path=None) -> int:
    """后台拉起，返回 PID。stdout/stderr 合并重定向到 log_path。

    关键：POSIX 用 start_new_session（脱离会话，Agent 之后可独立终止整组）；
    Windows 用 DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW。
    —— 这样进程不会随 Agent 会话结束被清掉。
    """
    slog = None
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        slog = open(log_path, "ab")
    kwargs = dict(
        cwd=str(cwd) if cwd else None,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=slog if slog else subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        argv = [str(c) for c in cmd]
        if os.name == "nt":
            flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
            p = subprocess.Popen(argv, creationflags=flags, **kwargs)
        else:
            p = subprocess.Popen(argv, start_new_session=True, **kwargs)
        return p.pid
    finally:
        if slog:
            try:
                slog.close()
            except OSError:
                pass


def _wait_gone(pid, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_alive(pid)


def _taskkill(pid, force: bool) -> None:
    cmd = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        cmd.append("/F")
    try:
        subprocess.run(cmd, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def terminate_pid(pid, grace: float = 8.0) -> bool:
    """优雅停止，超时则强杀。返回是否已停止。

    POSIX 用进程组（我们在 spawn 时建了独立会话），连带子进程一起收；
    Windows 用 taskkill /T 杀进程树。
    """
    if not pid_alive(pid):
        return True
    if os.name == "nt":
        _taskkill(pid, force=False)
        if _wait_gone(pid, grace):
            return True
        _taskkill(pid, force=True)
        return _wait_gone(pid, 3.0)
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            return True
    if _wait_gone(pid, grace):
        return True
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass
    return _wait_gone(pid, 3.0)


# ---------------------------------------------------------------- 凭据

def restrict_permissions(path: Path) -> None:
    """尽力收紧文件权限（POSIX 600 / Windows ACL）。失败不致命。"""
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME")
            if user:
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                    capture_output=True, timeout=15,
                )
        else:
            os.chmod(path, 0o600)
    except (OSError, subprocess.SubprocessError):
        pass


def ensure_secret(manifest: dict | None = None) -> tuple[Path, bool]:
    """确保本地固定 JWT secret 存在。返回 (路径, 是否新建)。

    为什么必须固定：不设 SKILLOS_JWT_SECRET 时后端用进程随机值，
    一重启所有 token 失效、daemon 得重新配对（09 §1.2 / 04 §4.1）。
    """
    m = manifest or load_manifest()
    name = m["paths"].get("secret_file", "secret")
    p = state_dir(m) / name
    if p.exists() and p.read_text(encoding="utf-8").strip():
        return p, False
    p.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    restrict_permissions(p)
    return p, True


def daemon_token_path() -> Path:
    """daemon 长期 token 的落盘位置（与 daemon/main.py:token_path 一致）。"""
    home = os.environ.get("SKILLOS_ROOT") or str(Path.home())
    return Path(home) / ".skillos" / "token"


# ---------------------------------------------------------------- 运行时解析

def vendor_uv(pk: str | None = None) -> Path | None:
    pk = pk or platform_key()
    p = vendor_dir() / pk / ("uv.exe" if os.name == "nt" else "uv")
    return p if p.exists() else None


def vendor_python_dir(pk: str | None = None) -> Path:
    pk = pk or platform_key()
    return vendor_dir() / pk / "python"


def vendor_mingit(pk: str | None = None) -> Path | None:
    pk = pk or platform_key()
    d = vendor_dir() / pk / "MinGit"
    return d if (d / "cmd" / "git.exe").exists() or (d / "mingw64").exists() else None


# 托管 CPython（uv 装出来的）与便携 CPython 的目录形态一致：
#   vendor/<平台>/python/cpython-3.11.17-windows-x86_64-none/python.exe
# 所以同一套 glob 两处通用。
_PY_GLOBS = (
    "bin/python3", "bin/python", "python.exe",
    "*/bin/python3", "*/bin/python", "*/python.exe",
    "*/*/bin/python3", "*/*/bin/python", "*/*/python.exe",
)


def find_python_in(root: Path) -> Path | None:
    """在目录（含一级/二级子目录）里找 Python 解释器；找不到返回 None。"""
    if not root.exists():
        return None
    for pat in _PY_GLOBS:
        for p in sorted(root.glob(pat)):
            if p.is_file():
                return p
    return None


def _python_candidates() -> list[tuple[list[str], str]]:
    cands: list[tuple[list[str], str]] = []
    if sys.executable:
        cands.append(([sys.executable], "sys.executable"))
    cands.append((["python3"], "python3"))
    cands.append((["python"], "python"))
    if os.name == "nt":
        cands.append((["py", "-3"], "py -3"))
    dedup, seen = [], set()
    for cmd, label in cands:
        key = tuple(cmd)
        if key in seen:
            continue
        seen.add(key)
        dedup.append((cmd, label))
    return dedup


def resolve_runtimes(manifest: dict | None = None, dev_mode: bool = False) -> dict:
    """**检测优先**：系统可达且达标 → 用系统；否则回落 vendor；都没有 → missing。

    返回结构（可安全 json.dumps）：
        {
          "platform": "win-x64",
          "python": {"source": "system"|"vendor"|"missing", ...},
          "git":    {"source": "system"|"vendor"|"missing", ...},
          "node":   {"source": "system"|"skipped"|"missing", ...},
          "notes":  [...]
        }
    """
    m = manifest or load_manifest()
    pk = platform_key()
    spec = m.get("runtimes", {})
    out = {"platform": pk, "python": {}, "git": {}, "node": {}, "notes": []}

    # ---- python
    py_spec = spec.get("python", {})
    pymin = py_spec.get("min", "3.11")
    pin = py_spec.get("pin")
    _ = pymin
    found = None
    rejected: list[str] = []
    for cmd, label in _python_candidates():
        ok_, ver = probe_version([*cmd, "-V"])
        if not ok_:
            continue
        pv = parse_version(ver)
        if pin:
            # ABI 锁定：frozen 形态的 .pyd 只认编译它的那个小版本。
            # 系统有 3.12/3.13 也不能用 —— 必须回落 vendor 自带的那个版本。
            if pv and pv[:2] == _pad(parse_version(pin))[:2]:
                found = {"source": "system", "argv": cmd, "via": label, "version": ver}
                break
            rejected.append(ver or label)
        elif version_ge(ver, pymin):
            found = {"source": "system", "argv": cmd, "via": label, "version": ver}
            break
    if found is None:
        uv = vendor_uv(pk)
        pydir = vendor_python_dir(pk)
        # 先看有没有**离线预置/随包**的托管 CPython —— 有它就是首选，不依赖 uv
        if find_python_in(pydir):
            found = {"source": "vendor", "uv": str(uv) if uv else None,
                     "python_dir": str(pydir), "version": f"managed@{pydir.name}"}
        elif uv:
            found = {"source": "vendor-uv-online", "uv": str(uv),
                     "python_dir": str(pydir), "version": None}
        else:
            out["notes"].append("未发现可用 Python，且 vendor 内无 uv —— 需人工安装 Python ≥ "
                                f"{pymin}")
    if rejected:
        out["notes"].append(
            f"系统 Python {'、'.join(rejected)} 已忽略：发行物锁定 CPython {pin}"
            f"（ABI 不符），改用 vendor 内自带的 {pin}")
    out["python"] = found or {"source": "missing"}

    # ---- git
    git_spec = spec.get("git", {})
    gitmin = git_spec.get("min")
    gfound = None
    gitpath = which("git")
    if gitpath:
        ok, ver = probe_version(["git", "--version"])
        if ok and (gitmin is None or version_ge(ver, gitmin)):
            gfound = {"source": "system", "path": gitpath, "version": ver}
    if gfound is None and pk in (git_spec.get("vendor_platforms") or []):
        mg = vendor_mingit(pk)
        if mg:
            gfound = {
                "source": "vendor",
                "path": str(mg / "cmd" / "git.exe"),
                "exec_path": str(mg / "mingw64" / "libexec" / "git-core"),
                "bin_dirs": [str(mg / "cmd"), str(mg / "mingw64" / "bin")],
                "version": None,
            }
    if gfound is None:
        out["notes"].append("未发现可用 git（Windows 可放 vendor/<平台>/MinGit 兜底）")
    out["git"] = gfound or {"source": "missing"}

    # ---- node（默认跳过：Web 走预构建 dist）
    node_spec = spec.get("node", {})
    if not dev_mode:
        out["node"] = {"source": "skipped", "reason": "非 dev 模式，Web 用预构建 dist"}
    else:
        nodemin = node_spec.get("min", "20")
        nfound = None
        np = which("node")
        if np:
            ok, ver = probe_version(["node", "-v"])
            if ok and version_ge(ver, nodemin):
                nfound = {"source": "system", "path": np, "version": ver}
        if nfound is None:
            out["notes"].append(f"--dev 模式需要 Node ≥ {nodemin}，未发现达标版本")
        out["node"] = nfound or {"source": "missing"}

    return out


# ---------------------------------------------------------------- 形态判定

def repo_root_from(start: Path | None = None) -> Path | None:
    """向上找 canonical 仓库根（有 pyproject.toml 且 name=skillos + skillos/ 目录）。"""
    start = Path(start or package_root()).resolve()
    for cand in [start, *start.parents]:
        py = cand / "pyproject.toml"
        if py.exists() and (cand / "skillos").is_dir():
            try:
                if 'name = "skillos"' in py.read_text(encoding="utf-8"):
                    return cand
            except OSError:
                return cand
    return None


def resolve_mode(manifest: dict | None = None) -> tuple[str, Path | None]:
    """返回 (mode, repo_root)。mode ∈ {"repo", "dist"}。

    manifest.mode = repo|dist|auto。auto：找得到仓库用 repo，否则 dist。
    """
    m = manifest or load_manifest()
    want = (m.get("mode") or "auto").lower()
    repo = repo_root_from()
    if want == "repo":
        return "repo", repo
    if want == "dist":
        return "dist", repo
    return ("repo", repo) if repo else ("dist", None)


def venv_dir(manifest: dict | None = None) -> Path:
    m = manifest or load_manifest()
    return state_dir(m) / ".venv"


# ---------------------------------------------------------------- 二进制形态
#
# 编译发行（docs/10）把 skillos 编成原生二进制，运行时不再需要 Python。
# 布局由 build/build.py 生成的 manifest.binary 描述：
#
#   "binary": {
#     "shared": true,
#     "roles": {"cli": {"dir": "runtime/skillos", "name": "skillos"}, ...}
#   }
#
# dir 相对 **本包根**（不是 CWD），这样整个包可任意搬移。

def binary_role(role: str, manifest: dict | None = None) -> Path | None:
    """解析二进制形态下某角色的可执行文件；不可用返回 None。"""
    m = manifest or load_manifest()
    spec = ((m.get("binary") or {}).get("roles") or {}).get(role)
    if not spec:
        return None
    base = (package_root() / spec["dir"]).resolve()
    name = spec["name"]
    for cand in (base / f"{name}.exe", base / name):
        if cand.is_file():
            return cand
    return None


def binary_roles(manifest: dict | None = None) -> dict:
    """一次性解析全部角色，供 detect / status 汇报。"""
    m = manifest or load_manifest()
    out: dict = {}
    for role in ("cli", "mcp", "server", "daemon"):
        p = binary_role(role, m)
        out[role] = {"path": str(p), "available": p is not None}
    return out


def is_binary_mode(manifest: dict | None = None) -> bool:
    """当前是否应走二进制形态。

    判据是「能不能真的找到 server 二进制」而不是「manifest 里写没写」——
    后者会在源码仓库里误判（仓库 manifest 带 binary 块只为记录布局）。
    """
    m = manifest or load_manifest()
    return binary_role("server", m) is not None


def binary_backend_argv(m: dict, host: str, port: int) -> list:
    """二进制形态的后端启动命令。

    与源码形态的关键差异：**端口走环境变量而非命令行参数**。
    编译入口 skillos_server.py 读 SKILLOS_HOST / SKILLOS_PORT；
    之所以不用 `--port`，是因为 uvicorn CLI 的字符串入口在编译产物里不可用
    （见 docs/10 §4「入口改写」）。
    """
    exe = binary_role("server", m)
    if exe is None:
        die("二进制形态下找不到 server 可执行文件。发行包可能不完整。")
    return [exe]


def binary_daemon_argv(m: dict, cloud: str, pair: str | None) -> list:
    """二进制形态的 daemon 启动命令（复用产品 CLI 的 `daemon` 子命令）。"""
    exe = binary_role("cli", m) or binary_role("daemon", m)
    if exe is None:
        die("二进制形态下找不到 cli 可执行文件。发行包可能不完整。")
    argv = [exe, "daemon", "--cloud", cloud]
    if pair:
        argv += ["--pair", pair]
    return argv


# ---------------------------------------------------------------- frozen 形态
#
# D1 发行（docs/10）：skillos 被编译成一个扩展模块（.pyd / .so），第三方依赖
# 交给随包 venv。布局由 build/build.py 生成的 manifest.frozen 描述：
#
#   "frozen": {
#     "dir": "runtime/skillos",           ← PYTHONPATH 注入点（.pyd 在这里）
#     "requirements": "runtime/requirements.txt",
#     "python_pin": "3.11"
#   }
#
# 与 binary 形态的区别：binary 连解释器都编进去了（连 Python 都不需要）；
# frozen 需要 Python（随包自带或系统同版本），但**不需要装 skillos**。
#
# 关键取舍：运行时靠 **PYTHONPATH 环境变量**接到 .pyd，而不是 `pip install`。
# 所以任何探测/拉起都必须用 `-P` 而不是 `-I` —— `-I` 隐含 `-E`，会把
# PYTHONPATH 一并忽略，导致「明明在跑却 import 不到」。

def frozen_spec(manifest: dict | None = None) -> dict:
    m = manifest or load_manifest()
    return (m.get("frozen") or {}) if m.get("mode") == "dist" else {}


def frozen_dir(manifest: dict | None = None) -> Path | None:
    """本包 runtime/skillos —— 编译产物的家，也是要注入 PYTHONPATH 的那个目录。

    路径取自 manifest.frozen.dir（相对本包根），所以改成包内子目录无需动脚本。
    """
    spec = frozen_spec(manifest)
    if not spec.get("dir"):
        return None
    return (package_root() / spec["dir"]).resolve()


def frozen_pythonpath(manifest: dict | None = None) -> str | None:
    d = frozen_dir(manifest)
    return str(d) if d and d.is_dir() else None


def frozen_requirements(manifest: dict | None = None) -> Path | None:
    spec = frozen_spec(manifest)
    if not spec.get("requirements"):
        return None
    return (package_root() / spec["requirements"]).resolve()


def frozen_extensions(manifest: dict | None = None) -> list[Path]:
    d = frozen_dir(manifest)
    if not d or not d.is_dir():
        return []
    return sorted([*d.glob("*.pyd"), *d.glob("*.so")])


def frozen_runtime_python(manifest: dict | None = None) -> Path | None:
    """发行物自带的便携 CPython（若有）。"""
    return find_python_in(vendor_python_dir())


def is_frozen_mode(manifest: dict | None = None) -> bool:
    """当前是否应走 frozen 形态。

    判据是「manifest 声明了 frozen 块 **且** 真的找得到编译产物」——
    与 is_binary_mode 同样的思路：只信实物，不信声明。
    """
    return bool(frozen_extensions(manifest))


def web_dist_candidates(repo: Path | None) -> list[Path]:
    """可能的 SPA 构建产物目录，按优先级排列。"""
    cands: list[Path] = []
    if repo:
        cands.append(repo / "web" / "dist")
    # 发行包布局（build.py 的装配结果）：载荷全在包内 runtime/ 下
    cands.append(package_root() / "runtime" / "web")
    cands.append(package_root() / "web_dist")
    cands.append(package_root() / "web" / "dist")
    # 旧布局兼容：<包>/../web（早期把 web 放在包外平级）
    cands.append(package_root().parent / "web")
    return cands


def web_dist_dir(repo: Path | None = None) -> Path | None:
    """找已构建好的 SPA 目录（含 index.html）；找不到返回 None。"""
    for c in web_dist_candidates(repo):
        if (c / "index.html").exists():
            return c
    return None


def web_src_dir(repo: Path | None = None) -> Path | None:
    """前端源码目录（构建/热更用）。"""
    if repo and (repo / "web" / "package.json").exists():
        return repo / "web"
    if (package_root() / "web" / "package.json").exists():
        return package_root() / "web"
    return None


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def venv_script(venv: Path, name: str) -> Path:
    sub = "Scripts" if os.name == "nt" else "bin"
    return venv / sub / (name + (".exe" if os.name == "nt" else ""))


# ---------------------------------------------------------------- 命令组装

def npm_argv(args: list[str]) -> list[str]:
    """Windows 上 npm 是 npm.cmd，必须经 cmd /c 才可执行。"""
    if os.name == "nt":
        return ["cmd", "/c", "npm", *args]
    return ["npm", *args]


def daemon_argv(venv: Path, cloud: str, pair: str | None) -> list[str]:
    """daemon 启动命令：优先用 venv 内 console script，缺失则退回 python -c。

    退回分支带 `-P`：不把 cwd 塞进 sys.path（防误命中源码目录），
    但保留 PYTHONPATH —— frozen 形态的 skillos 就是靠它加载的。
    """
    script = venv_script(venv, "skillos")
    if script.exists():
        argv = [str(script), "daemon", "--cloud", cloud]
    else:
        argv = [str(venv_python(venv)), "-P", "-c",
                "from skillos.cli.main import app; app()", "daemon", "--cloud", cloud]
    if pair:
        argv += ["--pair", pair]
    return argv


def backend_argv(venv: Path, host: str, port: int, reload: bool = False,
                 reload_dir: str | None = None) -> list[str]:
    argv = [str(venv_python(venv)), "-m", "uvicorn", "skillos.api.asgi:app",
            "--host", host, "--port", str(port)]
    if reload:
        argv.append("--reload")
        if reload_dir:
            argv += ["--reload-dir", reload_dir]
    return argv


# ---------------------------------------------------------------- 配对

def pairing_code(cloud_http: str, email: str, password: str,
                 timeout: float = 10.0) -> str:
    """自动配对：注册（409 幂等）→ 登录 → 申请 6 位码。返回码。"""
    try:
        http_json(cloud_http, "POST", "/api/auth/register",
                  {"email": email, "password": password, "name": "SkillOS"},
                  timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 = 账号已存在（重复运行），继续登录
            raise
    d = http_json(cloud_http, "POST", "/api/auth/login",
                  {"email": email, "password": password}, timeout=timeout)
    token = (d.get("data") or {}).get("token")
    if not token:
        raise RuntimeError(f"登录失败，响应：{d}")
    d = http_json(cloud_http, "POST", "/api/pairing/code", {}, token=token,
                  timeout=timeout)
    code = (d.get("data") or {}).get("code")
    if not code:
        raise RuntimeError(f"未取得配对码，响应：{d}")
    return str(code)


# ---------------------------------------------------------------- 输出

def setup_stdio() -> None:
    """Windows 控制台按 UTF-8 输出，避免中文/符号触发 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1):
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
