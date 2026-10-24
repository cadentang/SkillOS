#!/usr/bin/env python3
"""release_pull.py —— 拉取 SkillOS 发行产物，归位 vendor/dist/

归属：skillos-builder skill（触发词「创建SKILLOS」「更新SKILLOS」）。
归档目标默认为同级产品包的 `<script>/../../skillos/vendor/dist/`，**只保留最新版本**
（拉取前自动清空旧内容）。

两条通道（默认 auto：先 Release，失败再回退 artifact）：

  ① Release（**免 token**，公开仓库匿名可下载 —— 正常发版走这条）::

        python scripts/release_pull.py                     # 最新一次发行
        python scripts/release_pull.py --tag v0.2.2        # 指定版本
        python scripts/release_pull.py --list              # 只看清单，不下载
        python scripts/release_pull.py --jobs 8            # 并发连接数（默认 3）

  ② Actions artifact（**必须 token**；只有早期没挂 Release 的 run 才用）::

        SKILLOS_GH_TOKEN=github_pat_xxx python scripts/release_pull.py --source artifact
        SKILLOS_GH_TOKEN=xxx python scripts/release_pull.py --run 38063781796
        echo <token> | python scripts/release_pull.py --source artifact --token-stdin

为什么优先 Release：artifact 下载**强制要 token**（公开仓库也 401），把「拉产物」
这一步永远卡在人工提供凭据上；Release assets 匿名可取、且不受 14 天保留期限制。

行为：
  1. 选版本：Release 列表最新非预发布（或 --tag / --run 指定）
  2. **并发**下载 4 个 `skillos-<版本>-<平台>.zip` + `web-dist.zip`（默认 5 连接）
  3. Release 通道：asset 即交付 zip，直接归档；
     artifact 通道：内层还有一层目录结构，只取其中交付 zip
  4. 清掉归档区旧内容（只有最新版本）
  5. 写 RELEASE-INFO.json（版本 / 来源 / run / commit / sha256 清单），供事后核验

为什么并发：下载是 302 → Azure 签名 CDN，**单连接常被限速**。实测同一批
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
from datetime import UTC, datetime
from pathlib import Path

API = "https://api.github.com"
WEB = "https://github.com"
WORKFLOW = "release.yml"
PLATFORMS = ("win-x64", "mac-arm64", "mac-x64", "linux-x64")
PLATFORM_ARTIFACTS = {f"skillos-{p}" for p in PLATFORMS}
WEB_ARTIFACT = "web-dist"
WEB_ASSET = "web-dist.zip"
DELIVERY_ZIP_RE = re.compile(
    r"^skillos-(\d+\.\d+\.\d+)-(win-x64|mac-arm64|mac-x64|linux-x64)\.zip$"
)
TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")


def default_target() -> Path:
    """<本 skill>/../../skillos/vendor/dist —— 归档进产品包 vendor，不动产品定义。"""
    return Path(__file__).resolve().parent.parent.parent / "skillos" / "vendor" / "dist"


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def die(msg: str, code: int = 2) -> None:
    print(f"[release_pull] {msg}", file=sys.stderr)
    raise SystemExit(code)


def read_token(args: argparse.Namespace) -> str:
    """token 只在内存里；为空是常态（Release 通道不需要）。"""
    tok = os.environ.get("SKILLOS_GH_TOKEN", "").strip()
    if not tok and args.token_stdin:
        tok = sys.stdin.readline().strip()
    return tok


def require_token(token: str) -> str:
    if token:
        return token
    die(
        "artifact 通道必须 token。**正常发版不需要**：去掉 --source artifact 即走 Release"
        "（公开仓库匿名可下载）。确需拉早期 artifact 时，生成 fine-grained token"
        "（仅 SkillOS 仓库、Permissions → Actions → Read-only、7 天过期），"
        "用 SKILLOS_GH_TOKEN=<token> 或 --token-stdin 传入。token 只在本次进程内使用，"
        "不落盘；用完建议吊销。",
    )
    return token  # pragma: no cover —— die() 不会返回


def http_json(url: str, token: str | None = None, soft: bool = False) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "skillos-release-pull"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        if soft:
            print(f"[release_pull] 警告：{url} → {type(e).__name__}: {e}（继续）", file=sys.stderr)
            return {}
        die(f"请求失败 {url} → {type(e).__name__}: {e}")
        return {}


def pick_release(repo: str, tag: str | None, token: str | None) -> dict | None:
    """挑一个已发布的 Release（非 draft / 非 prerelease）。没有就返回 None。"""
    rels = http_json(f"{API}/repos/{repo}/releases?per_page=30", token)
    if not isinstance(rels, list):
        return None
    items = [r for r in rels if not r.get("draft") and not r.get("prerelease")]
    if tag:
        for r in items:
            if r.get("tag_name") == tag:
                return r
        return None
    for r in items:
        if TAG_RE.fullmatch(r.get("tag_name", "")):
            return r
    return None


def download(url: str, dest: Path, token: str | None, kind: str, retries: int = 3,
             expected: int | None = None) -> None:
    """kind=artifact：两段式（带 token 拿 302 Location，再**不带鉴权头**取 CDN）。
    kind=release：普通跟随重定向（匿名单，任何鉴权头都会被 CDN 拒）。

    两者都带**续传重试**：GitHub 的 CDN 会在大文件慢速传输时直接掐连接
    （实测 RemoteDisconnected），此时用 Range 从断点接着下，不必从头再来。

    `expected` 是 API 声明的大小。**必须校验**：连接被掐断时 urlopen 可能正常
    EOF 而不抛异常（实测 linux 包停在正好 60 MiB 仍被当成功），只有比大小才发现。
    """
    import urllib.error

    last = ""
    for attempt in range(1, retries + 1):
        have = dest.stat().st_size if dest.exists() else 0
        if expected is not None and have > expected:
            dest.unlink()                       # 超出预期 = 上一轮脏数据，重来
            have = 0
        if expected is not None and have == expected:
            return
        try:
            if kind == "release":
                headers = {"User-Agent": "skillos-release-pull"}
                if have:
                    headers["Range"] = f"bytes={have}-"
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=300) as r:
                    partial = r.status == 206 and have > 0
                    with open(dest, "ab" if partial else "wb") as f:
                        shutil.copyfileobj(r, f, 1 << 20)
            else:
                _artifact_stream(url, token, dest, have)
            size = dest.stat().st_size
            if expected is not None and size != expected:
                raise OSError(f"大小不符：{size:,} ≠ {expected:,}（{size / 1048576:.1f} MB）")
            return
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            got = dest.stat().st_size if dest.exists() else 0
            if attempt < retries:
                print(f"    重试 {attempt}/{retries}：{last}（已有 {got / 1048576:.1f} MB，续传）",
                      file=sys.stderr)
                time.sleep(min(20, 4 * attempt))
    die(f"下载失败 {url} → {last}（已重试 {retries} 次）")


def _artifact_stream(url: str, token: str | None, dest: Path, have: int) -> None:
    """artifact 通道：带鉴权拿 302，再裸取 CDN；带 Range 续传。"""
    import urllib.error

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    headers = {"Authorization": f"Bearer {token}", "User-Agent": "skillos-release-pull"}
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(urllib.request.Request(url, headers=headers), timeout=120)
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            die(f"下载失败 {url} → HTTP {e.code}: {e.reason}")
        loc = e.headers.get("Location")
        e.close()
        if not loc:
            die(f"下载失败 {url} → 302 但无 Location")
        h2 = {"User-Agent": "skillos-release-pull"}
        if have:
            h2["Range"] = f"bytes={have}-"
        with urllib.request.urlopen(urllib.request.Request(loc, headers=h2), timeout=900) as r:
            partial = r.status == 206 and have > 0
            with open(dest, "ab" if partial else "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
        return
    with resp:
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f, 1 << 20)


def pick_run(repo: str, token: str, run_id: int | None) -> dict:
    if run_id:
        run = http_json(f"{API}/repos/{repo}/actions/runs/{run_id}", token)
        if run.get("conclusion") != "success":
            die(f"run {run_id} 状态为 {run.get('conclusion')}，不是 success —— 不拉失败的产物。")
        return run
    runs = http_json(
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


def commit_from_zip(zip_path: Path) -> str:
    """从交付 zip 内的 BUILD-INFO.json 取 CI 记录的 commit（Release 通道没有 run id）。"""
    try:
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                if n.endswith("BUILD-INFO.json"):
                    return json.loads(z.read(n)).get("git_commit", "")
    except Exception:  # noqa: BLE001
        return ""
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", default="cadentang/SkillOS")
    ap.add_argument("--tag", default=None,
                    help="指定发行 tag（如 v0.2.2）；默认取最新 Release")
    ap.add_argument("--run", type=int, default=None,
                    help="指定 run id（强制走 artifact 通道，需要 token）")
    ap.add_argument("--source", choices=["auto", "release", "artifact"], default="auto",
                    help="产物来源：auto（默认，先 Release 再回退 artifact）/ release / artifact")
    ap.add_argument("--list", action="store_true", help="只列出将拉取的文件，不下载")
    ap.add_argument("--jobs", type=int, default=3,
                    help="并发下载连接数（默认 3；连接太多单条被 CDN 掐断，慢时可调到 5-8）")
    ap.add_argument("--retries", type=int, default=3,
                    help="单个文件失败重试次数（带 Range 续传，默认 3）")
    ap.add_argument("--token-stdin", action="store_true", help="从 stdin 第一行读 token")
    ap.add_argument("--target", default=None,
                    help="归档目录（默认 <repo>/skill/skillos/vendor/dist/）")
    args = ap.parse_args()

    token = read_token(args)
    out_dir = Path(args.target) if args.target else default_target()

    source = args.source
    if args.run:
        source = "artifact"          # 指定 run 只能走 artifact
    rel: dict | None = None
    if source in ("auto", "release"):
        rel = pick_release(args.repo, args.tag, token or None)
        if rel:
            source = "release"
        elif source == "release":
            die(f"仓库 {args.repo} 没有匹配 {args.tag or '最新'} 的 Release。"
                "确认该 tag 的 release.yml 里 publish job 是否跑过。")
        else:
            source = "artifact"
            print("[release_pull] 未找到 Release 附件 → 回退 artifact 通道（需要 token）")
    if source == "artifact":
        token = require_token(token)

    # ---------------- 组装下载清单 ----------------
    items: list[dict] = []
    version = ""
    tag = ""
    run: dict = {}
    release_url = ""

    if source == "release":
        rel = rel or {}
        tag = rel.get("tag_name", "")
        release_url = rel.get("html_url", "")
        m = TAG_RE.fullmatch(tag)
        if not m:
            die(f"Release tag 形态异常：{tag}")
        version = m.group(1)
        by_name = {a["name"]: a for a in (rel.get("assets") or [])}
        for plat in PLATFORMS:
            nm = f"skillos-{version}-{plat}.zip"
            a = by_name.get(nm)
            if not a:
                have = sorted(n for n in by_name if DELIVERY_ZIP_RE.fullmatch(n))
                die(f"Release {tag} 缺 {nm}；现有交付 zip：{have}")
            items.append({"name": nm, "url": a["browser_download_url"],
                          "size": a["size"], "kind": "release"})
        wa = by_name.get(WEB_ASSET)
        if not wa:
            die(f"Release {tag} 缺 {WEB_ASSET}")
        items.append({"name": WEB_ASSET, "url": wa["browser_download_url"],
                      "size": wa["size"], "kind": "release"})
        # run 溯源（公开仓库匿名可读；拿不到不影响归档）
        rr = http_json(f"{API}/repos/{args.repo}/actions/workflows/{WORKFLOW}"
                       f"/runs?branch={tag}&per_page=1", token or None, soft=True)
        hits = rr.get("workflow_runs") or []
        run = hits[0] if hits else {}
        print(f"Release {tag} | {len(items)} 个附件 | {release_url}")
    else:
        run = pick_run(args.repo, token, args.run)
        branch = run.get("head_branch", "")
        tag = branch if TAG_RE.fullmatch(branch) else ""
        arts = http_json(
            f"{API}/repos/{args.repo}/actions/runs/{run['id']}/artifacts?per_page=50", token,
        )
        allowed = PLATFORM_ARTIFACTS | {WEB_ARTIFACT}
        wanted = [a for a in arts.get("artifacts", []) if a["name"] in allowed]
        missing = allowed - {a["name"] for a in wanted}
        if missing:
            die(f"run {run['id']} 缺 artifact：{sorted(missing)} —— 不做半套归档。")
        for a in wanted:
            items.append({
                "name": a["name"],
                "url": f"{API}/repos/{args.repo}/actions/artifacts/{a['id']}/zip",
                "size": a["size_in_bytes"], "kind": "artifact",
            })
        print(f"run {run['id']} | {branch} | commit {run.get('head_sha', '')[:12]}"
              f" | {run.get('created_at')}")

    for it in items:
        print(f"  {it['name']:<24} {it['size']:>12,} bytes  [{it['kind']}]")

    if args.list:
        print(f"[dry] 将写入 {out_dir}（先清旧，只留最新）")
        return

    # --- 下载到临时目录，全部成功后才动归档区 ---
    # 并发下载：走 302 → Azure 签名 CDN，**单连接常被限速**
    # （实测同一批 390MB：单线程 51 KB/s ≈ 2 小时）。失败不落半套归档。
    tmp = Path(tempfile.mkdtemp(prefix="skillos-release-"))
    staged: dict[str, Path] = {}
    jobs = max(1, min(args.jobs, len(items)))
    print(f"并发下载 {len(items)} 个文件（{jobs} 连接）…")
    try:
        raws: dict[str, Path] = {}
        t0 = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            futs = {
                pool.submit(download, it["url"], tmp / f"{it['name']}.raw",
                            token or None, it["kind"], max(1, args.retries),
                            it["size"]): it
                for it in items
            }
            errors: list[str] = []
            for fut in concurrent.futures.as_completed(futs):
                it = futs[fut]
                try:
                    fut.result()
                except BaseException as e:  # die() 抛的是 SystemExit，一并收集
                    errors.append(f"{it['name']}: {e}")
                    continue
                p = tmp / f"{it['name']}.raw"
                raws[it["name"]] = p
                print(f"  ✓ {it['name']:<24} {p.stat().st_size / 1048576:7.1f} MB"
                      f"  ({time.monotonic() - t0:.0f}s)")
            if errors:
                die("下载失败：\n      " + "\n      ".join(errors))

        for it in items:
            raw = raws[it["name"]]
            if it["kind"] == "release":
                staged[it["name"]] = raw       # Release asset 本身就是交付 zip
                continue
            if it["name"] == WEB_ARTIFACT:
                staged[WEB_ASSET] = raw
                continue
            with zipfile.ZipFile(raw) as z:
                members = [n for n in z.namelist() if DELIVERY_ZIP_RE.match(Path(n).name)]
                if len(members) != 1:
                    die(f"{it['name']} artifact 内交付 zip 数量异常：{members}")
                member = members[0]
                dest = tmp / Path(member).name
                dest.write_bytes(z.read(member))
                staged[dest.name] = dest

        # 结构校验：截断的 zip 也可能「下载成功」，必须真的打开一次
        for nm, p in sorted(staged.items()):
            try:
                with zipfile.ZipFile(p) as z:
                    _ = len(z.namelist())
            except zipfile.BadZipFile as e:
                die(f"{nm} 不是完整 zip（{p.stat().st_size:,} 字节）：{e} —— 拒绝归档。")

        # 版本一致性：4 个交付 zip 的版本号必须一致，且与 tag 对齐
        vers = {DELIVERY_ZIP_RE.fullmatch(n).group(1)
                for n in staged if DELIVERY_ZIP_RE.fullmatch(n)}
        if len(vers) != 1:
            die(f"交付 zip 版本不一致：{sorted(vers)}")
        version = vers.pop()
        tag = tag or f"v{version}"
        if tag.lstrip("v") != version:
            die(f"tag {tag} 与交付 zip 版本 {version} 不一致 —— 拒绝归档。")

        # --- 全部就绪：清旧、落新（归档区只留最新版本） ---
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.iterdir():
            if old.is_dir():
                shutil.rmtree(old)
            else:
                old.unlink()

        head_sha = (run.get("head_sha") or "")[:12]
        if not head_sha:                      # Release 通道可能查不到 run，退回包内 BUILD-INFO
            for nm, p in sorted(staged.items()):
                if DELIVERY_ZIP_RE.fullmatch(nm):
                    head_sha = commit_from_zip(p)[:12]
                    break
        info = {
            "version": version,
            "tag": tag,
            "source": source,
            "release_url": release_url,
            "run_id": run.get("id"),
            "run_url": run.get("html_url", ""),
            "head_sha": head_sha,
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
    print("核验要点：RELEASE-INFO.json 的 head_sha 应等于 tag 指向的提交；"
          "sha256 可与 CI 侧 BUILD-INFO 交叉核对。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
