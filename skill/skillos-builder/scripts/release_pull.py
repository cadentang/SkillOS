#!/usr/bin/env python3
"""release_pull.py —— 从 GitHub Actions 拉取 SkillOS 发行产物，归位 vendor/dist/

归属：skillos-builder skill（触发词「创建SKILLOS」「更新SKILLOS」）。
归档目标默认为同级产品包的 `<script>/../../skillos/vendor/dist/`，**只保留最新版本**
（拉取前自动清空旧内容）。

用法（token 绝不写盘、不进日志、不打印）：

    SKILLOS_GH_TOKEN=github_pat_xxx python scripts/release_pull.py            # 拉最新成功 run
    SKILLOS_GH_TOKEN=xxx python scripts/release_pull.py --run 38063781796     # 指定 run id
    SKILLOS_GH_TOKEN=xxx python scripts/release_pull.py --list                # 只看清单，不下载
    SKILLOS_GH_TOKEN=xxx python scripts/release_pull.py --jobs 8              # 并发连接数（默认 5）
    echo <token> | python scripts/release_pull.py --token-stdin               # 管道传 token

行为：
  1. 选 run：release.yml 最近一次 success（或 --run 指定）
  2. **并发**下载 4 个 skillos-<平台> artifact + web-dist（默认 5 连接）
  3. 平台 artifact 内含两层：交付 zip（skillos-<版本>-<平台>.zip）+ 解包目录；
     只取**交付 zip** 存入归档区；web-dist 原样存为 web-dist.zip
  4. 清掉归档区旧内容（只有最新版本）
  5. 写 RELEASE-INFO.json（版本 / run / commit / sha256 清单），供事后核验

为什么并发：artifact 下载是 302 → Azure 签名 CDN，**单连接常被限速**。实测同一批
390MB（4 平台 + web-dist），单线程只有 51 KB/s（约 2 小时），并发后恢复数倍。
下载全部成功才会动归档区 —— 中途失败不会留下半套归档。

退出码：0 成功；2 参数/网络/token 问题；1 拉到了但校验不过。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"
WORKFLOW = "release.yml"
PLATFORM_ARTIFACTS = {"skillos-win-x64", "skillos-mac-arm64", "skillos-mac-x64", "skillos-linux-x64"}
WEB_ARTIFACT = "web-dist"
DELIVERY_ZIP_RE = re.compile(r"^skillos-(\d+\.\d+\.\d+)-(win-x64|mac-arm64|mac-x64|linux-x64)\.zip$")


def default_target() -> Path:
    """<本 skill>/../../skillos/vendor/dist —— 归档进产品包 vendor，不动产品定义。"""
    return Path(__file__).resolve().parent.parent.parent / "skillos" / "vendor" / "dist"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def die(msg: str, code: int = 2) -> "None":
    print(f"[release_pull] {msg}", file=sys.stderr)
    raise SystemExit(code)


def get_token(args: argparse.Namespace) -> str:
    tok = os.environ.get("SKILLOS_GH_TOKEN", "").strip()
    if not tok and args.token_stdin:
        tok = sys.stdin.readline().strip()
    if not tok:
        die(
            "缺少 token。请生成 fine-grained token（仅 SkillOS 仓库、Permissions → Actions → Read-only、"
            "7 天过期），然后 SKILLOS_GH_TOKEN=<token> 传入或 --token-stdin 管道传入。"
            "token 只在本次进程内使用，不落盘；用完建议吊销。",
        )
    return tok


def api_json(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "skillos-release-pull",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        die(f"请求失败 {url} → {type(e).__name__}: {e}")


def api_download(url: str, token: str, dest: Path) -> None:
    """artifact 下载是 302 → Azure 签名 CDN。必须两段式：带 token 拿 Location，
    再**不带任何鉴权头**取 CDN —— 否则 Bearer 头跟着跳过去会导致 CDN 401。"""
    import urllib.error

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        url, headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "skillos-release-pull",
        },
    )
    try:
        try:
            resp = opener.open(req, timeout=120)
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                die(f"下载失败 {url} → HTTP {e.code}: {e.reason}")
            loc = e.headers.get("Location")
            e.close()
            if not loc:
                die(f"下载失败 {url} → 302 但无 Location")
            with urllib.request.urlopen(loc, timeout=900) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            return
        with resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        die(f"下载失败 {url} → {type(e).__name__}: {e}")


def pick_run(repo: str, token: str, run_id: int | None) -> dict:
    if run_id:
        run = api_json(f"{API}/repos/{repo}/actions/runs/{run_id}", token)
        if run.get("conclusion") != "success":
            die(f"run {run_id} 状态为 {run.get('conclusion')}，不是 success —— 不拉失败的产物。")
        return run
    runs = api_json(
        f"{API}/repos/{repo}/actions/workflows/{WORKFLOW}/runs?status=success&per_page=10", token,
    )
    items = runs.get("workflow_runs") or []
    if not items:
        die(f"仓库 {repo} 没有成功的 {WORKFLOW} run。")
    return items[0]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default="cadentang/SkillOS")
    ap.add_argument("--run", type=int, default=None, help="指定 run id；缺省取最新成功的 release run")
    ap.add_argument("--list", action="store_true", help="只列出将拉取的 artifact，不下载")
    ap.add_argument("--jobs", type=int, default=5,
                    help="并发下载连接数（默认 5；单连接被 CDN 限速时调大更快，1 = 串行）")
    ap.add_argument("--token-stdin", action="store_true", help="从 stdin 第一行读 token")
    ap.add_argument("--target", default=None, help="归档目录（默认 <repo>/skill/skillos/vendor/dist/）")
    args = ap.parse_args()

    token = get_token(args)
    run = pick_run(args.repo, token, args.run)
    rid, branch, sha = run["id"], run.get("head_branch", ""), run.get("head_sha", "")[:12]
    tag = branch if re.fullmatch(r"v\d+\.\d+\.\d+", branch) else ""
    print(f"run {rid} | {branch} | commit {sha} | {run.get('created_at')}")

    arts = api_json(f"{API}/repos/{args.repo}/actions/runs/{rid}/artifacts?per_page=50", token)
    wanted = [a for a in arts.get("artifacts", []) if a["name"] in PLATFORM_ARTIFACTS | {WEB_ARTIFACT}]
    found = {a["name"] for a in wanted}
    missing = (PLATFORM_ARTIFACTS | {WEB_ARTIFACT}) - found
    if missing:
        die(f"run {rid} 缺 artifact：{sorted(missing)} —— 不做半套归档。")
    for a in wanted:
        print(f"  {a['name']:<20} {a['size_in_bytes']:>12,} bytes  id={a['id']}")

    out_dir = Path(args.target) if args.target else default_target()
    if args.list:
        print(f"[dry] 将写入 {out_dir}（先清旧，只留最新）")
        return

    # --- 下载到临时目录，全部成功后才动归档区 ---
    # 并发下载：artifact 走 302 → Azure 签名 CDN，**单连接常被限速**
    # （实测同一批 390MB：单线程 51 KB/s ≈ 2 小时）。失败不落半套归档。
    tmp = Path(tempfile.mkdtemp(prefix="skillos-release-"))
    staged: dict[str, Path] = {}
    version: str | None = None
    jobs = max(1, min(args.jobs, len(wanted)))
    print(f"并发下载 {len(wanted)} 个 artifact（{jobs} 连接）…")
    try:
        raws: dict[str, Path] = {}
        t0 = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            futs = {
                pool.submit(
                    api_download,
                    f"{API}/repos/{args.repo}/actions/artifacts/{a['id']}/zip",
                    token,
                    tmp / f"{a['name']}.artifact.zip",
                ): a
                for a in wanted
            }
            errors: list[str] = []
            for fut in concurrent.futures.as_completed(futs):
                a = futs[fut]
                try:
                    fut.result()
                except BaseException as e:  # die() 抛的是 SystemExit，一并收集
                    errors.append(f"{a['name']}: {e}")
                    continue
                p = tmp / f"{a['name']}.artifact.zip"
                raws[a["name"]] = p
                print(f"  ✓ {a['name']:<20} {p.stat().st_size / 1048576:7.1f} MB"
                      f"  ({time.monotonic() - t0:.0f}s)")
            if errors:
                die("下载失败：\n      " + "\n      ".join(errors))

        # 解包（本地 IO，顺序做。平台 artifact 是两层结构，见 references/release.md）
        for a in wanted:
            raw = raws[a["name"]]
            if a["name"] == WEB_ARTIFACT:
                staged["web-dist.zip"] = raw
                continue
            with zipfile.ZipFile(raw) as z:
                members = [n for n in z.namelist() if DELIVERY_ZIP_RE.match(Path(n).name)]
                if len(members) != 1:
                    die(f"{a['name']} artifact 内交付 zip 数量异常：{members}")
                member = members[0]
                ver = DELIVERY_ZIP_RE.match(Path(member).name).group(1)
                version = version or ver
                if ver != version:
                    die(f"版本不一致：{member} 里是 {ver}，此前是 {version}")
                dest = tmp / Path(member).name
                dest.write_bytes(z.read(member))
                staged[dest.name] = dest

        if version and tag and tag.lstrip("v") != version:
            die(f"tag {tag} 与交付 zip 版本 {version} 不一致 —— 拒绝归档。")

        # --- 全部就绪：清旧、落新（归档区只留最新版本） ---
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.iterdir():
            if old.is_dir():
                shutil.rmtree(old)
            else:
                old.unlink()

        info = {
            "version": version,
            "tag": tag,
            "run_id": rid,
            "run_url": run.get("html_url", ""),
            "head_sha": sha,
            "fetched_at": now_iso(),
            "files": {},
        }
        for name in sorted(staged):
            dest = out_dir / name
            shutil.move(str(staged[name]), dest)
            info["files"][name] = {"bytes": dest.stat().st_size, "sha256": sha256_file(dest)}
        write_json(out_dir / "RELEASE-INFO.json", info)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n=== 归档完成 → {out_dir}（仅最新 v{version}） ===")
    for name, meta in sorted(info["files"].items()):
        print(f"  {name:<36} {meta['bytes']:>12,}  sha256={meta['sha256'][:16]}…")
    print("核验要点：RELEASE-INFO.json 的 head_sha 应等于 tag 指向的提交；sha256 可与 CI 侧 BUILD-INFO 交叉核对。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
