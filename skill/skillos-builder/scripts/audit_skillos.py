#!/usr/bin/env python3
"""audit_skillos.py —— SkillOS 交付前审查（只读，绝不修改任何文件）

归属：skillos-builder skill。**skillos 最终要交付给客户**，所以每次创建/更新后
必须跑本审查：确认产品包「流程自洽」（SKILL.md 描述 ↔ 脚本实现 ↔ manifest ↔ 实物产物）
且「与项目约定不冲突」（端口 / ABI / 路径 / 依赖上限 / 红线）。

用法：
    python scripts/audit_skillos.py                          # 审 ../skillos 源码形态
    python scripts/audit_skillos.py --package <包根>          # 审指定包根（发行包解包目录）
    python scripts/audit_skillos.py --archive <交付 zip>      # 直接审交付 zip 内部
    python scripts/audit_skillos.py --repo <仓库根>           # 指定仓库根（查 docs/pyproject 约定）
    python scripts/audit_skillos.py --skip-release            # 跳过 vendor/dist 归档核对
    python scripts/audit_skillos.py --json                    # 机器可读输出

退出码：0 通过（可含 warn）；1 有 fail（**不得交付**）；2 参数/环境问题。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

LEVELS = {"ok": 0, "info": 0, "warn": 1, "fail": 2}
RESULTS: list[tuple[str, str, str]] = []   # (level, check_id, message)


def rec(level: str, cid: str, msg: str) -> None:
    RESULTS.append((level, cid, msg))


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------- 结构类

REQUIRED_SCRIPTS = [
    "detect.py", "bootstrap.py", "start.py", "status.py", "stop.py", "vendor_fetch.py",
]
REQUIRED_REFS = ["troubleshooting.md", "agent-config.md"]


def check_structure(pkg: Path) -> None:
    skill_md = pkg / "SKILL.md"
    if not skill_md.is_file():
        rec("fail", "SKILL-MD", f"缺少 {skill_md}")
    else:
        txt = _read(skill_md)
        m = re.match(r"^---\n(.*?)\n---\n", txt, re.S)
        if not m:
            rec("fail", "SKILL-MD", "SKILL.md 缺 frontmatter（--- name/description ---）")
        else:
            fm = m.group(1)
            for key in ("name:", "description:"):
                if key not in fm:
                    rec("fail", "SKILL-MD", f"frontmatter 缺 `{key}`")
            desc = re.search(r"description:\s*(.+)", fm)
            if desc:
                d = desc.group(1)
                for kw in ("启动", "停止"):
                    if kw not in d:
                        rec("warn", "SKILL-MD", f"description 未覆盖「{kw}」类场景：{d[:60]}…")
                rec("ok", "SKILL-MD", f"frontmatter 完整（name/description，{len(d)} 字）")

    if not (pkg / "manifest.json").is_file():
        rec("fail", "MANIFEST", "缺少 manifest.json")
    else:
        try:
            man = json.loads(_read(pkg / "manifest.json"))
            need = ["name", "launcher_version", "mode", "ports", "paths", "runtimes", "components"]
            miss = [k for k in need if k not in man]
            if miss:
                rec("fail", "MANIFEST", f"manifest.json 缺字段：{miss}")
            else:
                rec("ok", "MANIFEST", "manifest.json 必备字段齐全")
        except json.JSONDecodeError as e:
            rec("fail", "MANIFEST", f"manifest.json 解析失败：{e}")

    miss = [s for s in REQUIRED_SCRIPTS if not (pkg / "scripts" / s).is_file()]
    if miss:
        rec("fail", "SCRIPTS", f"缺少 scripts/：{miss}")
    else:
        rec("ok", "SCRIPTS", f"六个入口脚本齐（{', '.join(REQUIRED_SCRIPTS)}）")

    miss = [r for r in REQUIRED_REFS if not (pkg / "references" / r).is_file()]
    if miss:
        rec("fail", "REFS", f"缺少 references/：{miss}")
    else:
        rec("ok", "REFS", "references/ 两份文档齐")

    miss = [v for v in ("README.md", "manifest.json") if not (pkg / "vendor" / v).is_file()]
    if miss:
        rec("warn", "VENDOR-DOCS", f"缺少 vendor/：{miss}")
    else:
        rec("ok", "VENDOR-DOCS", "vendor 规约文件齐")


# ------------------------------------------------- 文档 ↔ 实现 一致性

FLAG_CANDIDATES = [
    "--dev", "--offline", "--mirror", "--no-daemon", "--open", "--port", "--email",
    "--password", "--json", "--host", "--list", "--hash", "--platform", "--yes",
    "--no-bootstrap", "--force", "--only", "--allow-unverified",
]


def check_doc_impl_consistency(pkg: Path) -> None:
    skill_txt = _read(pkg / "SKILL.md")
    scripts_dir = pkg / "scripts"
    if not skill_txt or not scripts_dir.is_dir():
        rec("warn", "DOC-IMPL", "SKILL.md 或 scripts/ 缺失，跳过一致性检查")
        return

    # 1) SKILL.md 引用的脚本必须真实存在
    refs = sorted(set(re.findall(r"scripts/([a-z_]+\.py)", skill_txt)))
    missing = [r for r in refs if not (scripts_dir / r).is_file()]
    if missing:
        rec("fail", "DOC-IMPL", f"SKILL.md 引用了不存在的脚本：{missing} ⇒ 文档承诺了做不到的事")
    else:
        rec("ok", "DOC-IMPL", f"SKILL.md 引用的 {len(refs)} 个脚本全部存在")

    # 2) 脚本孤儿检查（_common.py 为内部库，豁免）
    on_disk = {p.name for p in scripts_dir.glob("*.py")} - {"_common.py", "__init__.py"}
    orphan = sorted(on_disk - set(refs))
    if orphan:
        rec("warn", "DOC-IMPL", f"scripts/ 下未被 SKILL.md 提及：{orphan} ⇒ 文档可能滞后")
    else:
        rec("ok", "DOC-IMPL", "无孤儿脚本（每个脚本都在 SKILL.md 有出处）")

    # 2b) SKILL.md 引用到的所有文件必须真实存在（交付包里最容易漏的一项）
    #     注意分形态：runtime/、vendor/<平台>/、start.*、BUILD-INFO.json 是构建期注入，
    #     只在发行包里要求；其余（SKILL.md / manifest / references / scripts / vendor 规约）
    #     两种形态都必须有。
    DIST_ONLY = (
        "runtime/", "vendor/win-x64/", "vendor/mac-arm64/", "vendor/mac-x64/",
        "vendor/linux-x64/", "vendor/linux-arm64/", "start.cmd", "start.sh",
        "BUILD-INFO.json",
    )
    # vendor/dist/ 是 skillos-builder 的归档区（.gitignore + build.py 装配时整棵清掉），
    # **从不随包分发**：文档提到它只是「手工补装可选路径」，不算承诺包内存在。
    OPTIONAL = ("vendor/dist/", "vendor/dist")
    is_dist = (pkg / "BUILD-INFO.json").is_file()
    ref_paths = sorted(set(re.findall(
        r"`((?:references|scripts|vendor|runtime)/[A-Za-z0-9_./-]+?)`", skill_txt
    )))
    ref_paths += [
        p for p in ("manifest.json", "SKILL.md", "start.cmd", "start.sh") if f"`{p}`" in skill_txt
    ]
    absent = [p for p in ref_paths if not (pkg / p).exists()]
    optional_absent = [p for p in absent if p.startswith(OPTIONAL)]
    must_have = [p for p in absent if not p.startswith(DIST_ONLY + OPTIONAL)]
    dist_only_absent = [
        p for p in absent if p.startswith(DIST_ONLY) and not p.startswith(OPTIONAL)
    ]
    if must_have:
        rec("fail", "DOC-IMPL",
            f"SKILL.md 引用了包内不存在的文件：{must_have} ⇒ 客户按文档找不到东西")
    else:
        rec("ok", "DOC-IMPL", f"SKILL.md 引用的 {len(ref_paths)} 个包内路径全部存在")
    if optional_absent:
        rec("info", "DOC-IMPL",
            f"文档提到的可选归档不随包分发（正常）：{optional_absent}")
    if dist_only_absent:
        lvl = "fail" if is_dist else "info"
        where = "发行包中缺失" if is_dist else "源码形态下正常缺失"
        rec(lvl, "DOC-IMPL", f"构建期注入项在{where}：{dist_only_absent}")

    # 3) SKILL.md 承诺的命令行开关必须在脚本里真实存在
    script_text = "\n".join(_read(p) for p in scripts_dir.glob("*.py"))
    flag_pat = r"(?<![\w-])%s(?![\w-])"
    promised = [f for f in FLAG_CANDIDATES if re.search(flag_pat % re.escape(f), skill_txt)]
    phantom = [f for f in promised if f not in script_text]
    if phantom:
        rec("fail", "DOC-IMPL", f"SKILL.md 写了但脚本未实现的参数：{phantom} ⇒ 客户照文档敲会报错")
    else:
        rec("ok", "DOC-IMPL", f"文档承诺的 {len(promised)} 个参数在脚本中均有实现")


# ------------------------------------------------- 形态与实物

def check_form(pkg: Path) -> dict:
    """调 detect.py 拿形态；返回 detect 结果（失败给空 dict）。"""
    det = pkg / "scripts" / "detect.py"
    if not det.is_file():
        return {}
    try:
        out = subprocess.run(
            [sys.executable, str(det), "--json"], capture_output=True, text=True,
            timeout=120, cwd=str(pkg),
        )
        data = json.loads(out.stdout or "{}")
    except Exception:  # noqa: BLE001
        rec("warn", "FORM", "detect.py --json 未跑通，跳过形态判定")
        return {}
    form = (data.get("distribution") or data.get("form") or "").lower()
    frozen = data.get("frozen") or {}
    is_dist = (pkg / "BUILD-INFO.json").is_file()
    products = list(pkg.glob("runtime/**/*.pyd")) + list(pkg.glob("runtime/**/*.so"))
    if is_dist:
        if not products:
            rec("fail", "FORM", "发行包（有 BUILD-INFO.json）里找不到编译产物 .pyd/.so")
        else:
            names = ", ".join(sorted(p.name for p in products))
            rec("ok", "FORM", f"发行包形态：编译产物 {len(products)} 个（{names[:80]}）")
    else:
        if frozen.get("dir"):
            rec("fail", "FORM", f"源码包却声明 frozen.dir={frozen.get('dir')} ⇒ 形态自相矛盾")
        elif products:
            rec("warn", "FORM", "源码包里出现编译产物（可能是本地构建残留，勿入库）")
        else:
            rec("ok", "FORM", f"源码包形态正确（无 frozen 载荷；detect 判据='{form or '未标注'}'）")
    return data


def check_abi(pkg: Path) -> None:
    """frozen/python_tag 与实物名的 ABI 一致性（两种写法都要认）。"""
    man_path = pkg / "manifest.json"
    if not man_path.is_file():
        return
    man = json.loads(_read(man_path))
    frozen = man.get("frozen") or {}
    tag = frozen.get("python_tag")
    pin = frozen.get("python_pin")
    if not tag:
        rec("info", "ABI", "frozen.python_tag 为空（源码形态正常）")
        return
    forms = {tag}
    m = re.fullmatch(r"cp(\d)(\d+)", str(tag))
    if m:
        forms.add(f"cpython-{m.group(1)}{m.group(2)}")
    products = list(pkg.glob("runtime/**/*.pyd")) + list(pkg.glob("runtime/**/*.so"))
    if not products:
        return
    bad = [p.name for p in products if not any(f in p.name for f in forms)]
    if bad:
        rec("fail", "ABI", f"产物名与 python_tag={tag} 不符：{bad}")
    else:
        rec("ok", "ABI", f"ABI 一致（python_tag={tag}，pin={pin}，实物 {products[0].name}）")


def check_zero_install(pkg: Path) -> None:
    """发行包必须零安装：start.cmd/start.sh + 便携 CPython + web/dist。"""
    if not (pkg / "BUILD-INFO.json").is_file():
        rec("info", "ZERO-INSTALL", "源码包不要求零安装附件（发行包才强制）")
        return
    need = [pkg / "start.cmd", pkg / "start.sh", pkg / "runtime" / "web"]
    miss = [str(p.relative_to(pkg)) for p in need if not p.exists()]
    vendor_py = list(pkg.glob("vendor/*/python/python*"))
    if not vendor_py:
        miss.append("vendor/<平台>/python/")
    if miss:
        rec("fail", "ZERO-INSTALL", f"发行包缺零安装件：{miss} ⇒ 客户机器开箱跑不起来")
    else:
        rec("ok", "ZERO-INSTALL", "零安装件齐（start.cmd/start.sh + 便携 CPython + runtime/web）")


def check_source_leak(pkg: Path) -> None:
    if not (pkg / "runtime").is_dir():
        rec("info", "LEAK", "无 runtime/（源码形态正常）")
        return
    py = list((pkg / "runtime").rglob("*.py"))
    if py:
        shown = [str(p.relative_to(pkg)) for p in py[:3]]
        rec("fail", "LEAK", f"runtime/ 下出现源码 .py（{len(py)} 个，客户可见）：{shown}")
    else:
        rec("ok", "LEAK", "runtime/ 下无 .py 源码泄漏")


# ------------------------------------------------- 安全与项目冲突

SECRET_PATTERNS = [
    (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub fine-grained token"),
    (r"ghp_[A-Za-z0-9]{20,}", "GitHub classic token"),
    (r"\bsk-[A-Za-z0-9]{20,}", "OpenAI 风格密钥"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS Access Key"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥块"),
]
TEXT_GLOBS = [
    "SKILL.md", "manifest.json", "README.md", "README.txt",
    "*.md", "scripts/*.py", "references/*.md",
]


def check_secrets(pkg: Path) -> None:
    hits: list[str] = []
    for g in TEXT_GLOBS:
        for p in sorted(pkg.glob(g)):
            if not p.is_file() or p.suffix.lower() == ".zip":
                continue
            txt = _read(p)
            for pat, label in SECRET_PATTERNS:
                if re.search(pat, txt):
                    hits.append(f"{p.relative_to(pkg)} ← {label}")
    if hits:
        rec("fail", "SECRETS", f"包内出现疑似凭据，立即删除并吊销：{hits}")
    else:
        rec("ok", "SECRETS", f"文本面扫描无凭据痕迹（{len(SECRET_PATTERNS)} 类模式）")


def check_demo_cred(pkg: Path) -> None:
    mp = pkg / "manifest.json"
    if not mp.is_file():
        return
    man = json.loads(_read(mp))
    acct = man.get("account") or {}
    if acct.get("password"):
        rec(
            "info", "DEMO-CRED",
            f"manifest.account 内置演示账号（{acct.get('email')}）——已知保留的决策项，"
            "交付话术必须写明这是演示凭据、部署后应立即改密",
        )
    else:
        rec("ok", "DEMO-CRED", "manifest.account 未内置密码")


def check_project_conflicts(repo: Path, pkg: Path) -> None:
    """源码仓库内才可核对：端口 / 状态目录 / ABI pin / 依赖上限 是否与项目约定一致。"""
    if not (repo / "pyproject.toml").is_file():
        rec("info", "CONFLICT", "非源码仓库（无 pyproject.toml），跳过项目约定核对")
        return
    man = json.loads(_read(pkg / "manifest.json")) if (pkg / "manifest.json").is_file() else {}
    docs = "\n".join(_read(p) for p in sorted((repo / "docs").glob("*.md")))
    pyproject = _read(repo / "pyproject.toml")

    # 端口：manifest 与 docs/09 一致
    port = (man.get("ports") or {}).get("backend")
    if port:
        if str(port) in docs:
            rec("ok", "CONFLICT", f"端口 {port} 与 docs/ 约定一致")
        else:
            rec("fail", "CONFLICT",
                f"manifest 端口 {port} 在 docs/ 中查无（预期 9080）⇒ 文档与实现分裂")

    # 状态目录：~/.skillos 前缀
    sd = (man.get("paths") or {}).get("state_dir", "")
    if sd and not sd.startswith("~/.skillos"):
        rec("fail", "CONFLICT", f"state_dir={sd} 偏离 `~/.skillos` 约定（docs/09 §隔离边界）")
    else:
        rec("ok", "CONFLICT", f"state_dir 符合 ~/.skillos 约定（{sd}）")

    # ABI pin：与 docs/10 的 3.11 一致
    pin = ((man.get("runtimes") or {}).get("python") or {}).get("min")
    if pin and "3.11" not in docs and "3.11" not in pyproject:
        rec("fail", "CONFLICT", f"python.min={pin} 在 docs/ 与 pyproject 中查无锚点")
    else:
        rec("ok", "CONFLICT", f"Python ABI 锚点一致（manifest={pin}；docs/10 与 pyproject 均可查）")

    # 依赖上限（工程约定：无上限 = CI 定时炸弹）
    for dep in ("nuitka", "ruff", "mcp"):
        m = re.search(rf'"{dep}([^"]*)"', pyproject)
        if m and "<" not in m.group(1):
            rec("warn", "CONFLICT", f"pyproject 依赖 {dep} 无上限：{m.group(1)}")
    else:
        rec("ok", "CONFLICT", "关键依赖已封顶（nuitka / ruff / mcp）")


# ------------------------------------------------- 归档 ↔ 产物 一致性

def check_release_archive(pkg: Path) -> None:
    dist = pkg / "vendor" / "dist"
    if not dist.is_dir():
        rec("warn", "RELEASE", f"归档区不存在：{dist}（先跑 release_pull.py）")
        return
    info_path = dist / "RELEASE-INFO.json"
    if not info_path.is_file():
        rec("fail", "RELEASE", "归档区缺 RELEASE-INFO.json ⇒ 无台账，无法核验来源")
        return
    info = json.loads(_read(info_path))
    files = info.get("files") or {}
    # 内容完整性
    bad = []
    for name, meta in files.items():
        p = dist / name
        if not p.is_file():
            bad.append(f"{name} 缺失")
            continue
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        if h != meta.get("sha256"):
            bad.append(f"{name} sha256 不符")
    if bad:
        rec("fail", "RELEASE", f"归档文件校验失败：{bad}")
    else:
        rec("ok", "RELEASE", f"归档 {len(files)} 个文件 sha256 全部复核通过")

    # 平台齐全
    want = {"win-x64", "mac-arm64", "mac-x64", "linux-x64"}
    have = set()
    for n in files:
        m = re.fullmatch(r"skillos-\d+\.\d+\.\d+-(.+)\.zip", n)
        if m:
            have.add(m.group(1))
    missing = want - have
    if missing or "web-dist.zip" not in files:
        web = "ok" if "web-dist.zip" in files else "缺"
        rec("fail", "RELEASE", f"归档缺件：平台 {sorted(missing)}；web-dist={web}")
    else:
        rec("ok", "RELEASE", "四平台 + web 前端齐全")

    # 与包内 BUILD-INFO 交叉核对 + nuitka 版本窗口
    for name in files:
        if not name.startswith("skillos-"):
            continue
        p = dist / name
        if not p.is_file():
            continue
        try:
            with zipfile.ZipFile(p) as z:
                inner = [n for n in z.namelist() if n.endswith("BUILD-INFO.json")]
                if not inner:
                    rec("fail", "RELEASE", f"{name} 内无 BUILD-INFO.json（无法核验 commit）")
                    continue
                bi = json.loads(z.read(inner[0]))
        except Exception as e:  # noqa: BLE001
            rec("warn", "RELEASE", f"{name} 读取失败：{e}")
            continue
        if info.get("head_sha", "")[:7] != str(bi.get("git_commit", ""))[:7]:
            rec("fail", "RELEASE",
                f"{name}: BUILD-INFO.git_commit={bi.get('git_commit')}"
                f" ≠ RELEASE-INFO.head_sha={info.get('head_sha')}")
        nk = str(bi.get("nuitka", ""))
        try:
            nkv = tuple(int(x) for x in nk.split(".")[:2])
        except ValueError:
            nkv = (0, 0)
        if not (nkv >= (4, 2) and nkv < (5, 0)):
            rec("fail", "RELEASE", f"{name}: nuitka {nk} 不在锁定窗口 >=4.2,<5")
        if not (bi.get("source_leak_check") or {}).get("clean"):
            rec("fail", "RELEASE", f"{name}: source_leak_check 非 clean")
        if nkv >= (4, 2):
            rec("ok", "RELEASE",
                f"{name}: commit {bi.get('git_commit')} / nuitka {nk} / 无源码泄漏")


# ------------------------------------------------- 归档 zip 内部审查

def audit_archive(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        root = names[0].split("/")[0] + "/" if names else ""
        skill_md = next((n for n in names if n.endswith("SKILL.md")), None)
        manifest = next((n for n in names if n.endswith("manifest.json")), None)
        bi = next((n for n in names if n.endswith("BUILD-INFO.json")), None)
        if not skill_md:
            if zip_path.name.startswith("skillos-"):
                rec("fail", "ARCHIVE", f"{zip_path.name} 内无 SKILL.md ⇒ 交付包不完整")
            else:
                # web-dist.zip 这类只是前端压缩包，本就没有 SKILL.md —— 不是交付缺陷
                rec("ok", "ARCHIVE",
                    f"{zip_path.name}: 非交付包（无 SKILL.md）—— 跳过包内核验")
            return
        if not manifest:
            rec("fail", "ARCHIVE", f"{zip_path.name} 内无 manifest.json")
        if not bi:
            rec("fail", "ARCHIVE", f"{zip_path.name} 内无 BUILD-INFO.json ⇒ 无法核验来源")
        else:
            data = json.loads(z.read(bi))
            rec("ok", "ARCHIVE",
                f"{zip_path.name}: commit {data.get('git_commit')} / nuitka {data.get('nuitka')}"
                f" / platform {data.get('platform')}")
            exes = list((data.get("executables") or {}).keys())
            if not exes:
                rec("fail", "ARCHIVE", f"{zip_path.name}: BUILD-INFO 未登记可执行文件")
            else:
                members = [n for n in names if Path(n).name in exes]
                if not members:
                    rec("fail", "ARCHIVE", f"{zip_path.name}: 登记的 {exes} 在包内找不到实物")
                else:
                    rec("ok", "ARCHIVE", f"{zip_path.name}: 可执行实物在包（{exes[0]}）")
        expect_root = "skillos-" + zip_path.name.split("-", 2)[-1].removesuffix(".zip")
        if root and root.rstrip("/") != expect_root:
            rec("info", "ARCHIVE", f"{zip_path.name}: 顶层目录 {root.rstrip('/')}（解压即得包根）")
        txt = z.read(skill_md).decode("utf-8", "replace")
        for pat, label in SECRET_PATTERNS:
            if re.search(pat, txt):
                rec("fail", "ARCHIVE", f"{zip_path.name} 的 SKILL.md 出现 {label}")


# ------------------------------------------------- 主流程

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    here = Path(__file__).resolve().parent.parent          # skillos-builder/
    ap.add_argument("--package", default=str(here.parent / "skillos"),
                    help="包根（默认同级 skillos/）")
    ap.add_argument("--archive", default=None, help="直接审交付 zip")
    ap.add_argument("--repo", default=str(here.parent.parent), help="仓库根（查 docs/pyproject）")
    ap.add_argument("--skip-release", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.archive:
        z = Path(args.archive)
        if not z.is_file():
            print(f"[audit] 归档不存在：{z}", file=sys.stderr)
            raise SystemExit(2)
        print(f"=== SkillOS 交付前审查（归档：{z.name}）===")
        audit_archive(z)
    else:
        pkg = Path(args.package).resolve()
        if not pkg.is_dir():
            print(f"[audit] 包目录不存在：{pkg}", file=sys.stderr)
            raise SystemExit(2)
        print(f"=== SkillOS 交付前审查（包：{pkg}）===")
        check_structure(pkg)
        check_doc_impl_consistency(pkg)
        check_form(pkg)
        check_abi(pkg)
        check_zero_install(pkg)
        check_source_leak(pkg)
        check_secrets(pkg)
        check_demo_cred(pkg)
        check_project_conflicts(Path(args.repo).resolve(), pkg)
        if not args.skip_release:
            check_release_archive(pkg)

    fails = [r for r in RESULTS if r[0] == "fail"]
    warns = [r for r in RESULTS if r[0] == "warn"]
    if args.json:
        print(json.dumps(
            {"results": [{"level": lv, "check": cid, "message": m} for lv, cid, m in RESULTS],
             "fails": len(fails), "warns": len(warns)},
            ensure_ascii=False, indent=2,
        ))
    else:
        icon = {"ok": "ok  ", "info": "info", "warn": "WARN", "fail": "FAIL"}
        for lv, cid, msg in RESULTS:
            print(f"  [{icon[lv]}] {cid:<13} {msg}")
        print(f"\n=== 结果：{len(RESULTS)} 项检查，fail {len(fails)} / warn {len(warns)} ===")
        if fails:
            print("⇒ 存在 fail 项：**不得交付**，逐条修复后重跑（源码改动需走流程 A 出新版本）。")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
