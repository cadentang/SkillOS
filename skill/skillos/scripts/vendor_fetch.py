"""获取 vendor 运行时（uv / MinGit / CPython）—— 按 vendor/manifest.json 下载、校验、解压。

默认**只打印计划不下载**，加 `--yes` 才真正执行。
`sha256` 为空的条目默认**拒绝安装**（除非 `--allow-unverified`）—— 宁可不下，不装来路不明的东西。

用法：
    python scripts/vendor_fetch.py --list                 # 看当前平台待办
    python scripts/vendor_fetch.py --hash uv.tar.gz       # 算 sha256，填回 manifest
    python scripts/vendor_fetch.py --platform win-x64 --yes
    python scripts/vendor_fetch.py --yes --only uv

离线机器：在联网机器上跑完，把整个 vendor/<平台>/ 目录拷过去即可。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402


def vendor_manifest() -> dict:
    p = C.vendor_dir() / "manifest.json"
    if not p.exists():
        C.die(f"找不到 {p}")
    return C.read_json(p, {}) or {}


def iter_artifacts(vm: dict, pk: str, only: str | None):
    for group, spec in vm.items():
        if group.startswith("_"):
            continue
        if only and group != only:
            continue
        art = (spec.get("artifacts") or {}).get(pk)
        if art:
            yield group, spec, art


def _safe_extract(archive: Path, kind: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if kind == "zip":
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
        return
    if kind in ("tar.gz", "tgz"):
        with tarfile.open(archive, "r:gz") as t:
            try:
                t.extractall(dest, filter="data")  # py3.12+ 防路径穿越
            except TypeError:
                t.extractall(dest)
        return
    C.die(f"不支持的归档类型：{kind}")


def _unwrap(root: Path) -> Path:
    """剥掉只有一层的目录包装（很多发行包外面套一层同名目录）。"""
    cur = root
    while True:
        entries = list(cur.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            cur = entries[0]
            continue
        return cur


def materialize(archive: Path, kind: str, into: Path) -> None:
    """把归档铺到 `into`（既可能是文件目标，也可能是目录目标）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _safe_extract(archive, kind, tmp)
        content = _unwrap(tmp)
        entries = list(content.iterdir())

        if into.exists():
            if into.is_dir():
                shutil.rmtree(into)
            else:
                into.unlink()
        into.parent.mkdir(parents=True, exist_ok=True)

        if len(entries) == 1 and entries[0].is_file():
            # 文件目标（如 uv / uv.exe）
            shutil.move(str(entries[0]), str(into))
        else:
            # 目录目标（如 MinGit/ 、python/）
            into.mkdir(parents=True, exist_ok=True)
            for e in entries:
                shutil.move(str(e), str(into / e.name))


def download(url: str, dest: Path) -> None:
    C.log(f"  下载 {url}")
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def cmd_list(vm: dict, pk: str) -> None:
    C.log(f"=== vendor 清单 · 目标平台 {pk} ===")
    any_row = False
    for group, spec, art in iter_artifacts(vm, pk, None):
        any_row = True
        into = C.vendor_dir() / art["into"]
        present = into.exists()
        locked = bool(art.get("sha256"))
        C.log(f"[{'x' if present else ' '}] {group:<7} v{spec.get('version', '?')}")
        C.log(f"      落位 : {art['into']}  {'已就位' if present else '缺失'}")
        C.log(f"      sha  : {'已锁定 ' + art['sha256'][:16] + '…' if locked else '未锁定（需先填）'}")
    if not any_row:
        C.log(f"（当前平台 {pk} 无 vendor 条目 —— 说明此平台靠系统运行时即可）")


def main() -> None:
    ap = argparse.ArgumentParser(description="获取 vendor 运行时")
    ap.add_argument("--list", action="store_true", help="列出清单与就位状态")
    ap.add_argument("--platform", default=None, help="覆盖平台键（默认本机）")
    ap.add_argument("--only", default=None, choices=["uv", "mingit", "python"],
                    help="只处理某一组")
    ap.add_argument("--yes", action="store_true", help="真正下载安装（默认只打印计划）")
    ap.add_argument("--allow-unverified", action="store_true",
                    help="允许安装未锁定 sha256 的条目（不推荐）")
    ap.add_argument("--hash", default=None, metavar="FILE", help="计算文件 sha256 后退出")
    args = ap.parse_args()

    C.setup_stdio()

    if args.hash:
        p = Path(args.hash)
        if not p.exists():
            C.die(f"文件不存在：{p}")
        C.log(f"{C.sha256_file(p)}  {p}")
        return

    vm = vendor_manifest()
    pk = args.platform or C.platform_key()

    if args.list:
        cmd_list(vm, pk)
        return

    plan = []
    for group, spec, art in iter_artifacts(vm, pk, args.only):
        into = C.vendor_dir() / art["into"]
        if into.exists():
            C.log(f"跳过 {group}：{art['into']} 已存在")
            continue
        if not art.get("sha256") and not args.allow_unverified:
            C.log(f"跳过 {group}：sha256 未锁定（先用 --hash 算好填回 manifest，"
                  f"或显式 --allow-unverified）")
            continue
        plan.append((group, spec, art, into))

    if not plan:
        C.log("没有需要下载的条目。")
        return

    C.log(f"计划安装 {len(plan)} 项（平台 {pk}）：")
    for group, spec, art, into in plan:
        C.log(f"  - {group} v{spec.get('version', '?')} → {art['into']}")

    if not args.yes:
        C.log("\n这是预览（未下载）。加 --yes 执行。")
        return

    for group, spec, art, into in plan:
        C.log(f"→ 安装 {group} …")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "pkg"
            download(art["url"], tmp)
            want = art.get("sha256")
            if want:
                got = C.sha256_file(tmp)
                if got.lower() != want.lower():
                    C.die(f"{group} sha256 校验失败！\n  期望 {want}\n  实际 {got}\n"
                          f"  已中止，未改动 vendor/。")
                C.log("  ✓ sha256 校验通过")
            else:
                C.log("  ⚠ 未校验（--allow-unverified）")
            materialize(tmp, art.get("kind", "zip"), into)
        C.log(f"  ✓ 落位 {into}")

    C.log("\n完成。可运行 python scripts/detect.py 确认已被识别。")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"vendor_fetch: {e}", file=sys.stderr)
        sys.exit(1)
