"""Git 封装（04 §2.2.3，FR-1.0.10）。全部 subprocess 调用 git，不自研 VCS。

接口收紧（OUT-14）：
- 只暴露具名方法（init/commit/log/show/snapshot_at/diff/rollback），任务表即白名单。
- 不提供 run(cmd, args) 通用透传 —— daemon 参数来自云端，通用透传等于
  「云端触发 → 本地任意 git 执行」通道。
- 块内三方合并（git merge-file --diff3）本期不实现（OUT-11）。

跨平台（05 P0-1 / P2-5）：
- 所有 git 调用显式传 -c 参数，不继承用户全局配置。
- 回滚策略：checkout + 新建 commit，不 rewrite history。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from skillos.core.model import Commit, SkillSnapshot

# 05 P0-1：显式覆盖用户全局 git 配置，保证行为可预期
GIT_BASE_ARGS: tuple[str, ...] = (
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.safecrlf=false",
    "-c",
    "core.quotepath=false",  # 中文文件名正常显示，否则是八进制乱码
)

DEFAULT_AUTHOR = "Skill OS <skillos@local>"


class GitError(Exception):
    """git 调用失败（非零退出码 / git 不存在）。"""


class GitRepo:
    """canonical 仓库（SSOT）的具名操作集。"""

    def __init__(self, repo_root: Path) -> None:
        # T1: repo_root 由调用方显式传入，不从环境变量 / 单例读取
        self.repo_root = repo_root

    def _git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        cmd = ["git", *GIT_BASE_ARGS, *args]
        try:
            proc = subprocess.run(  # noqa: S603 —— 参数白名单由具名方法约束（OUT-14）
                cmd, cwd=self.repo_root, capture_output=True, check=False
            )
        except FileNotFoundError as e:
            raise GitError("git not found in PATH") from e
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace')}")
        return proc

    # ---- 具名方法（新增能力加具名方法，禁止加通用透传）----

    def init(self) -> None:
        """git init + 写 repo 级配置（不动全局，05 P2-5）。"""
        raise NotImplementedError("P1 实现")

    def commit(self, paths: list[str], message: str, author: str = DEFAULT_AUTHOR) -> str:
        """提交指定路径，返回 commit sha。"""
        raise NotImplementedError("P1 实现")

    def log(self, path: str | None = None, limit: int = 20) -> list[Commit]:
        """提交历史（可按路径过滤）。"""
        raise NotImplementedError("P1 实现")

    def show(self, commit: str, path: str) -> bytes:
        """取某 commit 下某文件的字节内容。"""
        raise NotImplementedError("P1 实现")

    def snapshot_at(self, commit: str) -> SkillSnapshot:
        """取某一 commit 的完整 canonical 快照 —— base 重建的唯一入口（NFR-3.5）。

        base 不落盘，靠 render(canonical@installed_commit, installed_adapter_version) 现算（T6）。
        """
        raise NotImplementedError("P1 实现")

    def diff(self, base: str, head: str, path: str | None = None) -> str:
        """unified diff 文本。二进制文件第一版只显示「已变更」（T5）。"""
        raise NotImplementedError("P1 实现")

    def rollback(self, commit: str, paths: list[str]) -> str:
        """git checkout <commit> -- <paths> 后提交新 commit，不重写历史。"""
        raise NotImplementedError("P1 实现")
