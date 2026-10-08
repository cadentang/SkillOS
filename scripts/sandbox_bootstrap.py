"""沙盒容器启动编排（P7 / FR-3.10）。纯标准库，容器内零额外依赖。

两个子命令：

install-seed
    首次启动：seed → $SKILLOS_ROOT/.skillos-seed（reset 恢复源）+ runtime
    目录 + init/import/push（出厂态）。幂等：状态目录已存在即跳过
    （重启容器不重置评委的体验现场，重置只走 reset 接口）。

pair
    登录 demo 用户 → POST /api/auth/pairing/code → 打印 6 位码。
    daemon 用它完成首次配对；token 落盘后重启不再需要。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEMO_EMAIL = "demo@skillos.local"
DEMO_PASSWORD = "skillos-demo"


def _install_seed(root: Path, seed_src: Path) -> None:
    # 开发态直接跑脚本时包未安装：把项目根补进 sys.path（容器内 pip install 不走此分支）
    try:
        import skillos  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from skillos.services.engine import EnvPaths, LocalEngine

    state = root / ".skillos"
    if (state / "repo" / ".git").exists():
        print("install-seed: 状态已存在，跳过（重置走 POST /api/sandbox/reset）")
        return
    seed_dst = root / ".skillos-seed"
    if seed_dst.exists():
        shutil.rmtree(seed_dst)
    shutil.copytree(seed_src / "runtime", seed_dst / "runtime")
    # 首次启动：seed → runtime deploy 根（entrypoint 语义，同测试 fixture）
    runtimes = sorted(
        p.name for p in (seed_dst / "runtime").iterdir() if p.is_dir()
    )
    for key in runtimes:
        dst = root / f".{key}" / "skills"
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(seed_dst / "runtime" / key, dst, dirs_exist_ok=True)
    engine = LocalEngine(
        EnvPaths(repo_root=state / "repo", db_path=state / "skillos.db"),
        home=root, sandbox=True,
    )
    engine.init_env()
    plan = engine.import_skills()
    for s in plan.skills:
        report = engine.push_skill(s.slug)
        if not report.ok:
            raise SystemExit(f"install-seed: push 失败 {s.slug}: "
                             f"{[(o.error_code, o.detail) for o in report.outcomes]}")
    print(f"install-seed: 出厂态就绪（{len(plan.skills)} skill × {len(runtimes)} 端）")


def _http(base: str, method: str, path: str, body: dict | None = None,
          token: str | None = None) -> dict:
    req = urllib.request.Request(
        base.rstrip("/") + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _pair(cloud: str, email: str, password: str) -> None:
    try:
        _http(cloud, "POST", "/api/auth/register",
              {"email": email, "password": password, "name": "Demo"})
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 = demo 用户已建（重启幂等），继续登录
            raise
    data = _http(cloud, "POST", "/api/auth/login",
                 {"email": email, "password": password})
    token = data["data"]["token"]
    data = _http(cloud, "POST", "/api/auth/pairing/code", {}, token=token)
    print(data["data"]["code"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("install-seed")
    s1.add_argument("--root", type=Path, required=True)
    s1.add_argument("--seed", type=Path, required=True)

    s2 = sub.add_parser("pair")
    s2.add_argument("--cloud", required=True, help="云端 HTTP 基址")
    s2.add_argument("--email", default=DEMO_EMAIL)
    s2.add_argument("--password", default=DEMO_PASSWORD)

    args = ap.parse_args()
    if args.cmd == "install-seed":
        _install_seed(args.root, args.seed)
    else:
        _pair(args.cloud, args.email, args.password)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 —— entrypoint 输出根因后以非零退出
        print(f"sandbox_bootstrap: {e}", file=sys.stderr)
        sys.exit(1)
