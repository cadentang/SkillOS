"""setuptools 构建钩子：把前端构建产物装进 wheel（2026-10-17）。

**为什么需要这个文件**：`skillos/api/web.py::_candidate_dirs()` 的第一顺位
候选是 `skillos/web_dist`（wheel 形态的前端落点），但那个目录是**构建期
生成**的 —— 源码树里没有、也不入库（根 `.gitignore` 忽略）。没有这一步时
`pip install .` 装出来的包不含 SPA，用户 `skillos serve` 打开首页只有 404，
而且日志里的原因只是「前端未构建？」，对装 wheel 的人毫无意义。

两种输入形态：

1. 源码树有 `web/dist`（`npm run build` 的产物）→ 整棵拷进 `skillos/web_dist`，
   拷前先清空（带哈希的资源名每版都变，残留会变成孤儿文件）；
2. 没有 `web/dist` 但 `skillos/web_dist` 已存在（从 sdist 构建 wheel）→ 原样
   保留，产物由 sdist 携带（见 `MANIFEST.in` 的 `graft`）。**此时绝不能清空**，
   否则从 sdist 装出来的 wheel 又变成没有前端的包。

`sdist` 也挂同一个钩子，否则 `python -m build`（先 sdist 再 wheel）的链路里
前端会在第一跳就丢掉。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist

ROOT = Path(__file__).resolve().parent
WEB_SRC = ROOT / "web" / "dist"
WEB_DST = ROOT / "skillos" / "web_dist"


def stage_web_dist() -> bool:
    """把 `web/dist` 同步到 `skillos/web_dist`；返回是否发生了拷贝。

    判据是 `index.html` 真实存在 —— 只判目录存在会把空的 `dist/`
    （脚手架或上次清理残留）当产物拷进去，装出来的包仍然是白屏。
    """
    if not (WEB_SRC / "index.html").is_file():
        return False
    if WEB_DST.exists():
        shutil.rmtree(WEB_DST)
    shutil.copytree(WEB_SRC, WEB_DST)
    return True


class build_py(_build_py):
    def run(self) -> None:
        if stage_web_dist():
            print(f"[skillos] staged SPA: {WEB_SRC} -> {WEB_DST}", file=sys.stderr)
        else:
            # 不是错误：从 sdist 构建时本就没有 web/dist，产物已在 sdist 里
            has_dst = (WEB_DST / "index.html").is_file()
            print(
                f"[skillos] no web/dist to stage (prebundled={has_dst})",
                file=sys.stderr,
            )
        super().run()


class sdist(_sdist):
    def run(self) -> None:
        stage_web_dist()
        super().run()


setup(cmdclass={"build_py": build_py, "sdist": sdist})
