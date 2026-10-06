"""分发器（04 §2.2.2，FR-1.0.9 / NFR-3 / NFR-5）。

写入协议（Windows 安全，10 步，见 04 §2.2.2）：
  1 解析目标路径 → 2 备份到 .skillos/backups/ → 3 adapter.render_layout()
  4 写临时目录 → 5 回收未纳管资产（>阈值文件，同名冲突以 canonical 为准）
  6 哈希校验 → 7 两步交换（Windows rename 不可覆盖非空目录）
  8 更新 runtime_bindings（installed_commit / adapter_version / installed_hash）
  9 写 sync_records → 10 任一步失败从备份还原

幂等性（NFR-5）：以内容哈希为判据，纳管文件一致时跳过写入。

残留自检与恢复（FR-1.0.15）：
  ① 只有 target.old 无 target → 改回（回退到分发前）
  ② 有 target.old 且有 target  → 删 target.old（补清理）
  ③ 只有 .<name>.tmp-<rand>/  → 删残留
执行时机：doctor 显式检查 · daemon 启动 · 每次 push 前定向检查。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DistributeResult:
    skill_id: str
    runtime_key: str
    ok: bool
    from_state: str | None = None
    to_state: str | None = None
    changed_files: list[str] | None = None
    skipped_assets: list[str] | None = None  # 未纳管资产明细（FR-1.0.20）
    error: str | None = None


@dataclass(frozen=True)
class ResidueReport:
    """push 前 / 启动时的残留自检结果（FR-1.0.15）。"""

    target: Path
    form: int  # 1/2/3 对应上述三种残留形态；0 = 干净


def distribute(
    root: Path,
    skill_id: str,
    runtime_key: str,
    *,
    dry_run: bool = False,
) -> DistributeResult:
    """执行分发（root 显式传入，T1）。实现待 P1。"""
    raise NotImplementedError("P1 实现：10 步写入协议 + 原子交换 + 未纳管资产回收")


def check_residue(target: Path) -> ResidueReport:
    """残留自检（幂等，可重复执行）。实现待 P1。"""
    raise NotImplementedError("P1 实现：三种残留形态判定")


def recover_residue(report: ResidueReport) -> None:
    """按残留形态执行恢复（幂等）。实现待 P1。"""
    raise NotImplementedError("P1 实现：①改回 ②补清理 ③删 tmp")
