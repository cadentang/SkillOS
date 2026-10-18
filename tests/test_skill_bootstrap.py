"""Skill 包 `bootstrap.py` 的前端产物解析（2026-10-17）。

背景：交付归档 `vendor/dist/web-dist.zip` 里本来就有前端（CI 的 `web-dist`
artifact），但此前没有任何代码读它 —— 离线机/无 node 的用户手里有产物，却只会
看到「单端口不可用」。

覆盖四条路径：
    * 已有 dist → 直接复用，不碰归档
    * 没有 dist、有归档 → 解到 `<包>/web_dist`，解完能被 `web_dist_dir` 找到
    * 归档损坏 / 缺 index.html → 只告警，不抛异常、不阻断依赖安装
    * dev 模式 → 完全不碰（前端走 vite）

这些脚本只依赖标准库（这是设计前提），所以本文件也不需要任何第三方包。
"""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "skill" / "skillos" / "scripts"


def _load(fname: str, modname: str):
    """按文件路径加载脚本，避免依赖 sys.path 里的同名模块。"""
    spec = importlib.util.spec_from_file_location(modname, _SCRIPTS / fname)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


bootstrap = _load("bootstrap.py", "skillos_bootstrap_under_test")


def _make_zip(path: Path, files: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


@pytest.fixture
def pkg(tmp_path, monkeypatch):
    """假的 skill 包根目录：`<tmp>/skillos`，package_root() 指过去。"""
    root = tmp_path / "skillos"
    root.mkdir()
    monkeypatch.setattr(bootstrap.C, "package_root", lambda: root)
    return root


def _rt(node_source: str = "missing") -> dict:
    return {"platform": "win-x64", "python": {}, "git": {}, "node": {"source": node_source}}


def test_archive_path_is_vendor_dist(pkg) -> None:
    assert bootstrap.web_archive() == pkg / "vendor" / "dist" / "web-dist.zip"


def test_extracts_package_archive(pkg) -> None:
    """没有 dist、有归档 → 自动解到 `<包>/web_dist`，且 index.html 可用。"""
    _make_zip(
        pkg / "vendor" / "dist" / "web-dist.zip",
        {"index.html": b"<div id=root></div>", "assets/app.js": b"console.log(1)"},
    )
    bootstrap.ensure_web_dist(_rt(), None, dev=False, offline=True)

    assert (pkg / "web_dist" / "index.html").is_file()
    assert (pkg / "web_dist" / "assets" / "app.js").read_bytes() == b"console.log(1)"
    # 解完必须能被运行期的定位函数看到（start.py 就是靠它拿 SKILLOS_WEB_DIST）
    assert bootstrap.C.web_dist_dir(None) == pkg / "web_dist"


def test_bad_archive_does_not_raise(pkg) -> None:
    """坏 zip：只告警，不抛异常、不留下假 dist。"""
    bad = pkg / "vendor" / "dist" / "web-dist.zip"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not a zip at all")

    bootstrap.ensure_web_dist(_rt(), None, dev=False, offline=True)  # 不抛

    assert not (pkg / "web_dist" / "index.html").exists()
    assert bootstrap.C.web_dist_dir(None) is None


def test_archive_without_index_rejected(pkg) -> None:
    """zip 里没有 index.html（归档不完整）→ 不认定为可用产物。"""
    _make_zip(
        pkg / "vendor" / "dist" / "web-dist.zip",
        {"assets/app.js": b"console.log(1)"},
    )
    bootstrap.ensure_web_dist(_rt(), None, dev=False, offline=True)
    assert bootstrap.C.web_dist_dir(None) is None


def test_existing_dist_wins(pkg) -> None:
    """已有 web_dist 时不重解（避免每次 bootstrap 都把用户改过的产物盖掉）。"""
    (pkg / "web_dist").mkdir()
    (pkg / "web_dist" / "index.html").write_bytes(b"sentinel")
    _make_zip(
        pkg / "vendor" / "dist" / "web-dist.zip",
        {"index.html": b"from-archive"},
    )
    bootstrap.ensure_web_dist(_rt(), None, dev=False, offline=True)
    assert (pkg / "web_dist" / "index.html").read_bytes() == b"sentinel"


def test_dev_mode_skips_archive(pkg) -> None:
    """dev 模式走 vite，不该产生 web_dist（否则会和热更抢路由）。"""
    _make_zip(
        pkg / "vendor" / "dist" / "web-dist.zip",
        {"index.html": b"<div id=root></div>"},
    )
    bootstrap.ensure_web_dist(_rt(), None, dev=True, offline=True)
    assert not (pkg / "web_dist").exists()


def test_repo_archive_shape_if_present() -> None:
    """真实归档（本地拉过才有，gitignored）必须是「zip 根 = index.html」。"""
    archive = _SCRIPTS.parent / "vendor" / "dist" / "web-dist.zip"
    if not archive.is_file():
        pytest.skip("vendor/dist 是本地归档区（gitignored），未拉取时跳过")
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
    assert "index.html" in names
    assert any(n.startswith("assets/") for n in names)
