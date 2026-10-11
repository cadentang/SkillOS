#!/usr/bin/env python3
"""check_sync.py —— 发版前「四对齐」核对（只读，不写仓库、不改历史）

核对四个维度是否对齐：
    本地 HEAD  /  origin/main  /  待发 tag  /  已有归档 RELEASE-INFO.json

不一致一律 block（退出码 1），由 agent 用 AskUserQuestion 让用户拍板；
本脚本**不会**替用户做 pull / rebase / push -f / 换号。

用法：
    python scripts/check_sync.py                  # 默认先 fetch，再判定
    python scripts/check_sync.py --tag v0.2.0     # 额外校验待发版本号是否合法
    python scripts/check_sync.py --no-fetch       # 离线/沙箱内（refs 可能过期）
    python scripts/check_sync.py --json           # 机器可读输出
    python scripts/check_sync.py --selftest       # 判定逻辑正反例自证（完全不碰 git）

退出码：0 = 可继续发版 / 1 = 需用户拍板 / 2 = 脚本自身错误
环境变量：SKILLOS_GH_TOKEN（可选）用于查远程最新成功 run；只读内存，不落盘。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = "cadentang/SkillOS"
MAIN_REF = "origin/main"
ARCHIVE_REL = os.path.join("skill", "skillos", "vendor", "dist", "RELEASE-INFO.json")
VER_RE = re.compile(r"v\d+\.\d+\.\d+")

STANDARD_OPTIONS = [
    "① 拉取远程后重来（git pull --ff-only）",
    "② 推送本地覆盖远程",
    "③ 换新版本号重发",
    "④ 中止，用户自己处理（不自行 pull / rebase / push -f / 换号）",
]


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def finding(level, code, message):
    return {"level": level, "code": code, "message": message}


def git(args, cwd=None, timeout=180):
    """跑一条只读 git 命令，返回 (rc, stdout, stderr)；git 缺失返回 127。"""
    try:
        p = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "", "git 未安装或不在 PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)} 超时（{timeout}s）"
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def parse_ver(s):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (s or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def pick_version_tags(names):
    """只留 vX.Y.Z 形态，按版本号降序。"""
    return sorted(
        {n for n in names if VER_RE.fullmatch(n)}, key=lambda s: parse_ver(s), reverse=True
    )


def parse_remote_tags(text):
    out = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
            continue
        name = parts[1][len("refs/tags/"):]
        if name.endswith("^{}"):  # annotated tag 的 peeled 行
            name = name[:-3]
        if VER_RE.fullmatch(name):
            out.add(name)
    return sorted(out, key=lambda s: parse_ver(s), reverse=True)


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------
def fetch_latest_run(token, timeout=30):
    """查 release.yml 在 main 上最近一次成功的 run（需要 token）。"""
    url = (
        f"https://api.github.com/repos/{REPO}/actions/workflows/release.yml/runs"
        "?status=success&branch=main&per_page=1"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "skillos-builder-check-sync",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    runs = data.get("workflow_runs") or []
    if not runs:
        return None
    r0 = runs[0]
    return {
        "run_id": r0.get("id"),
        "head_sha": r0.get("head_sha"),
        "created_at": r0.get("created_at"),
        "html_url": r0.get("html_url"),
    }


def collect(fetch=True, tag=None, timeout=180):
    st = {"tag": tag, "fetch": "skipped", "notes": []}

    rc, root, err = git(["rev-parse", "--show-toplevel"])
    if rc != 0:
        raise RuntimeError(f"不在 git 仓库内：{err or root}")
    st["root"] = root

    if fetch:
        rc, out, err = git(["fetch", "origin", "--tags", "--prune"], cwd=root, timeout=timeout)
        if rc == 0:
            st["fetch"] = "ok"
        else:
            st["fetch"] = "failed"
            st["fetch_error"] = (err or out).splitlines()[:3]
            st["notes"].append(
                "fetch 失败 → 远程 refs 可能过期，判定仅供参考"
                "（沙箱内跑 SSH 会报 Host key verification failed）"
            )

    rc, st["branch"], _ = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    rc, st["head"], _ = git(["rev-parse", "--short", "HEAD"], cwd=root)
    rc, st["head_sha"], _ = git(["rev-parse", "HEAD"], cwd=root)

    rc, st["origin_main"], err = git(["rev-parse", "--short", MAIN_REF], cwd=root)
    st["has_main_ref"] = rc == 0 and bool(st["origin_main"])
    if not st["has_main_ref"]:
        st["origin_main"] = None
        st["origin_main_error"] = err

    st["ahead"] = st["behind"] = None
    if st["has_main_ref"]:
        rc, out, _ = git(["rev-list", "--left-right", "--count", f"{MAIN_REF}...HEAD"], cwd=root)
        if rc == 0 and out:
            left, _, right = out.partition("\t")
            try:
                st["behind"], st["ahead"] = int(left), int(right)
            except ValueError:
                pass
        rc, out, _ = git(["log", "--oneline", f"{MAIN_REF}..HEAD"], cwd=root)
        st["ahead_commits"] = out.splitlines()[:10]
        rc, out, _ = git(["log", "--oneline", f"HEAD..{MAIN_REF}"], cwd=root)
        st["behind_commits"] = out.splitlines()[:10]

    rc, out, _ = git(["status", "--porcelain"], cwd=root)
    st["dirty_files"] = out.splitlines() if rc == 0 else []
    st["dirty_count"] = len(st["dirty_files"])

    rc, out, _ = git(["tag", "--list", "v*", "--sort=-v:refname"], cwd=root)
    st["local_tags"] = pick_version_tags(out.splitlines()) if rc == 0 else []

    st["remote_tags"] = []
    if st["has_main_ref"]:
        rc, out, err = git(["ls-remote", "--tags", "origin"], cwd=root, timeout=60)
        if rc == 0:
            st["remote_tags"] = parse_remote_tags(out)
        else:
            st["notes"].append(f"ls-remote 失败：{(err or '').splitlines()[:1]}")

    # 归档台账
    st["archive"] = None
    arch_path = os.path.join(root, ARCHIVE_REL)
    if os.path.isfile(arch_path):
        try:
            with open(arch_path, encoding="utf-8") as fh:
                raw = json.load(fh)
            st["archive"] = {
                "path": arch_path,
                "version": raw.get("version"),
                "run_id": raw.get("run_id"),
                "head_sha": (raw.get("head_sha") or raw.get("commit") or "")[:40],
                "generated_at": raw.get("generated_at") or raw.get("created_at"),
            }
        except Exception as exc:  # 台账坏掉不该阻断核对，只提示
            st["notes"].append(f"RELEASE-INFO.json 解析失败：{exc}")

    # 归档 head_sha 是否在远程历史里（三态：True/False/None=本地没有该对象）
    st["archive_in_remote"] = None
    if st["archive"] and st["archive"]["head_sha"] and st["has_main_ref"]:
        sha = st["archive"]["head_sha"]
        rc, _, _ = git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=root)
        if rc == 0:
            rc, _, _ = git(["merge-base", "--is-ancestor", sha, MAIN_REF], cwd=root)
            st["archive_in_remote"] = rc == 0

    # 远程最新成功 run（有 token 才查）
    st["remote_run"] = None
    st["remote_run_error"] = None
    token = os.environ.get("SKILLOS_GH_TOKEN", "").strip()
    if token:
        try:
            st["remote_run"] = fetch_latest_run(token)
        except urllib.error.HTTPError as exc:
            st["remote_run_error"] = f"GitHub API HTTP {exc.code}"
        except Exception as exc:
            st["remote_run_error"] = f"{type(exc).__name__}: {exc}"

    return st


# --------------------------------------------------------------------------
# 判定（纯函数：喂 state，出 findings —— 供 --selftest 复用）
# --------------------------------------------------------------------------
def decide(st):
    f = []

    # 1) 工作区
    if st.get("dirty_count"):
        f.append(finding("block", "WORKTREE_DIRTY",
                         f"工作区有 {st['dirty_count']} 项未提交改动：先确认哪些要提交、哪些丢弃"))
    else:
        f.append(finding("ok", "WORKTREE_CLEAN", "工作区干净"))

    # 2) 与远程主干的同步关系
    if not st.get("has_main_ref", True):
        f.append(finding("block", "NO_MAIN_REF",
                         "本地没有 origin/main（remote 未配置 / 从未 fetch）→ 无法判定同步关系"))
    else:
        ahead, behind = st.get("ahead"), st.get("behind")
        if ahead is None or behind is None:
            f.append(finding("block", "SYNC_UNKNOWN", "无法解析 ahead/behind 计数"))
        elif ahead and behind:
            f.append(finding("block", "DIVERGED",
                             f"本地与 {MAIN_REF} 已分叉（领先 {ahead} / 落后 {behind}）"
                             "→ 必须先决定合流方式"))
        elif behind:
            f.append(finding("block", "BEHIND",
                             f"本地落后 {behind} 个提交 → 基于旧代码发版，产物会落后于远程"))
        elif ahead:
            f.append(finding("ok", "AHEAD",
                             f"本地领先 {ahead} 个提交（第 2 步推送即可）"))
        else:
            f.append(finding("ok", "ALIGNED", f"与 {MAIN_REF} 完全对齐"))

    # 3) 待发版本号
    tag = st.get("tag")
    if not tag:
        f.append(finding("info", "TAG_NOT_GIVEN",
                         "未指定待发版本号（--tag vX.Y.Z）；确认沿用还是发新版后再核一次"))
    else:
        ver = parse_ver(tag)
        if ver is None:
            f.append(finding("block", "TAG_FORMAT", f"版本号 `{tag}` 不是 vX.Y.Z 形态"))
        else:
            norm = f"v{ver[0]}.{ver[1]}.{ver[2]}"
            remote_tags = st.get("remote_tags") or []
            if norm in remote_tags:
                f.append(finding("block", "TAG_TAKEN",
                                 f"tag {norm} 已存在于远程 → 覆盖会触发新 run（旧 run 保留）"
                                 "，还是换号？"))
            else:
                f.append(finding("ok", "TAG_FREE", f"tag {norm} 在远程未被占用"))
            highest = remote_tags[0] if remote_tags else None
            if highest and ver < parse_ver(highest):
                f.append(finding("block", "VERSION_REGRESSION",
                                 f"待发 {norm} 低于远程最高 {highest} → 这是版本回退，确认意图"))
            local_highest = (st.get("local_tags") or [None])[0]
            if local_highest and ver < parse_ver(local_highest):
                f.append(finding("info", "VERSION_BELOW_LOCAL",
                                 f"待发 {norm} 低于本地最高 tag {local_highest}"))

    # 4) 归档一致性
    arch = st.get("archive")
    if not arch:
        f.append(finding("info", "NO_ARCHIVE", "尚无 vendor/dist 归档（RELEASE-INFO.json 不存在）"))
    else:
        asha = arch.get("head_sha") or ""
        head_sha = st.get("head_sha") or ""
        # 归档里的 sha 可能被写过短/长两种形态，用前缀互匹
        is_current = bool(asha) and bool(head_sha) and (
            head_sha.startswith(asha) or asha.startswith(head_sha)
        )
        if st.get("archive_in_remote") is False:
            f.append(finding("block", "ARCHIVE_NOT_IN_REMOTE",
                             f"归档 head {asha[:12]}（v{arch.get('version')}）"
                             "不在远程历史里 → 历史产物与远程代码不符"))
        elif st.get("archive_in_remote") is True:
            if is_current:
                f.append(finding("ok", "ARCHIVE_CURRENT", f"归档 head {asha[:12]} = 本地 HEAD"))
            else:
                f.append(finding("info", "ARCHIVE_STALE",
                                 f"归档 head {asha[:12]}（v{arch.get('version')}）不是当前 HEAD"
                                 " → 第 5 步重拉即可，不阻塞"))
        else:
            f.append(finding("info", "ARCHIVE_SHA_UNKNOWN",
                             "归档 head_sha 在本地找不到对应对象，无法判定（可能未 fetch）"))

    # 5) 远程最新成功 run vs 归档（需要 token）
    run = st.get("remote_run")
    if run:
        rsha = (run.get("head_sha") or "")[:12]
        asha = ((arch or {}).get("head_sha") or "")[:12]
        if rsha and rsha == asha:
            f.append(finding("ok", "ARCHIVE_UP_TO_DATE",
                             f"归档与远程最新成功 run（#{run.get('run_id')} @ {rsha}）一致"))
        else:
            f.append(finding("info", "ARCHIVE_BEHIND_RUN",
                             f"远程最新成功 run #{run.get('run_id')} @ {rsha}"
                             f" 比归档 {asha or '无'} 新 → 重拉即可"))
    elif st.get("remote_run_error"):
        f.append(finding("info", "RUN_CHECK_SKIPPED",
                         f"未查远程最新 run：{st['remote_run_error']}"))
    else:
        f.append(finding("info", "RUN_CHECK_SKIPPED",
                         "未查远程最新 run（未提供 SKILLOS_GH_TOKEN）"))

    return f


def blocked(findings):
    return [x for x in findings if x["level"] == "block"]


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def render(st, findings, as_json=False):
    blk = blocked(findings)
    if as_json:
        print(json.dumps({"state": st, "findings": findings,
                          "blocked": len(blk), "verdict": "block" if blk else "ok"},
                         ensure_ascii=False, indent=2))
        return

    print("=== SKILLOS 版本一致性核对（四对齐：本地 / origin/main / tag / 归档）===")
    print(f"仓库        {st.get('root')}")
    print(f"fetch       {st.get('fetch')}")
    print(f"分支        {st.get('branch')}")
    print(f"本地 HEAD   {st.get('head')}")
    print(f"远程 main   {st.get('origin_main') or '(缺失)'}")
    if st.get("ahead") is not None and st.get("behind") is not None:
        print(f"同步        ahead {st['ahead']} / behind {st['behind']}")
    print(f"本地 tag    {', '.join(st.get('local_tags') or []) or '(无)'}")
    print(f"远程 tag    {', '.join(st.get('remote_tags') or []) or '(无)'}")
    if st.get("tag"):
        print(f"待发 tag    {st['tag']}")
    arch = st.get("archive")
    if arch:
        print(f"归档        v{arch.get('version')} / run {arch.get('run_id')}"
              f" / head {(arch.get('head_sha') or '')[:12]}")
    else:
        print("归档        (无)")

    for n in st.get("notes") or []:
        print(f"note        {n}")

    print("\n--- 逐项判定 ---")
    tag_map = {"ok": " OK  ", "info": "INFO ", "block": "BLOCK"}
    for x in findings:
        print(f"[{tag_map.get(x['level'], x['level'])}] {x['code']:<22} {x['message']}")

    print()
    if blk:
        print(f"=== 结论：需要用户拍板（{len(blk)} 项 block）—— 不得自行处理 ===")
        print("用 AskUserQuestion 让用户从下列标准选项中选：")
        for opt in STANDARD_OPTIONS:
            print(f"  {opt}")
    else:
        print("=== 结论：四对齐通过，可以继续（第 1 步 lint → 第 2 步推送 → 第 3 步打 tag）===")


# --------------------------------------------------------------------------
# 自证：判定逻辑正反例（完全不碰 git）
# --------------------------------------------------------------------------
def _base_state():
    return {
        "root": "/repo", "fetch": "ok", "branch": "main",
        "head": "abc1234", "head_sha": "abc1234abcdef",
        "has_main_ref": True, "origin_main": "abc1234",
        "ahead": 0, "behind": 0, "dirty_count": 0,
        "local_tags": ["v0.1.0"], "remote_tags": ["v0.1.0"],
        "tag": "v0.2.0", "archive": None, "archive_in_remote": None,
        "remote_run": None, "remote_run_error": None, "notes": [],
    }


def selftest():
    cases = []

    def case(name, mutate, expect_codes, expect_block):
        st = _base_state()
        mutate(st)
        codes = [x["code"] for x in decide(st)]
        ok = all(c in codes for c in expect_codes) and len(blocked(decide(st))) == expect_block
        cases.append((name, ok, codes, expect_block))

    case("对齐+tag 合法 → 可继续", lambda s: None, ["ALIGNED", "WORKTREE_CLEAN", "TAG_FREE"], 0)
    case("本地领先 → 可继续", lambda s: s.update(ahead=2), ["AHEAD"], 0)
    case("本地落后 → BLOCK", lambda s: s.update(behind=3), ["BEHIND"], 1)
    case("分叉 → BLOCK", lambda s: s.update(ahead=1, behind=2), ["DIVERGED"], 1)
    case("工作区脏 → BLOCK", lambda s: s.update(dirty_count=4), ["WORKTREE_DIRTY"], 1)
    case("tag 远程已占用 → BLOCK",
         lambda s: s.update(remote_tags=["v0.1.0", "v0.2.0"]), ["TAG_TAKEN"], 1)
    case("版本回退 → BLOCK", lambda s: s.update(remote_tags=["v0.3.0"]), ["VERSION_REGRESSION"], 1)
    case("tag 形态非法 → BLOCK", lambda s: s.update(tag="0.2"), ["TAG_FORMAT"], 1)
    case("归档不在远程历史 → BLOCK",
         lambda s: s.update(archive={"version": "0.1.0", "run_id": 1, "head_sha": "deadbeefcafe"},
                            archive_in_remote=False),
         ["ARCHIVE_NOT_IN_REMOTE"], 1)
    case("归档落后于 HEAD → 仅 INFO",
         lambda s: s.update(archive={"version": "0.1.0", "run_id": 1, "head_sha": "old12345678"},
                            archive_in_remote=True),
         ["ARCHIVE_STALE"], 0)
    case("归档 = 远程最新 run → OK",
         lambda s: s.update(archive={"version": "0.1.0", "run_id": 77, "head_sha": "abc1234abcdef"},
                            archive_in_remote=True,
                            remote_run={"run_id": 77, "head_sha": "abc1234abcdef"}),
         ["ARCHIVE_UP_TO_DATE"], 0)
    case("无 origin/main → BLOCK",
         lambda s: s.update(has_main_ref=False, origin_main=None), ["NO_MAIN_REF"], 1)

    print("=== check_sync.py 自证（判定逻辑正反例）===")
    all_ok = True
    for name, ok, codes, exp in cases:
        all_ok &= ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<30} 期望 block {exp} | 命中 {codes}")
    print(f"\n{len(cases)} 个用例：{'全部通过' if all_ok else '有失败'}")
    return 0 if all_ok else 1


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="SKILLOS 发版前版本一致性核对（只读）")
    ap.add_argument("--tag", help="待发版本号，如 v0.2.0")
    ap.add_argument("--no-fetch", action="store_true", help="跳过 git fetch（离线/沙箱内）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--selftest", action="store_true", help="跑判定逻辑正反例自证")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    try:
        st = collect(fetch=not args.no_fetch, tag=args.tag)
    except Exception as exc:
        print(f"check_sync 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    findings = decide(st)
    render(st, findings, as_json=args.json)
    return 1 if blocked(findings) else 0


if __name__ == "__main__":
    sys.exit(main())
