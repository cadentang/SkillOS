"""漂移检测（04 §2.1.3，FR-1.0.6 / FR-1.0.7）。

判定分两步：归属判定（三事实）→ 内容三方比对（渲染后空间）。
本模块保持纯函数：哈希计算与状态判定不碰磁盘、不碰 git；
磁盘 / git 的事实由 core/fs 与 services 层取回后传入。
"""

from __future__ import annotations

import hashlib

from skillos.core.model import DriftState


# ---------------------------------------------------------------- 哈希口径


def normalize_bytes(content: bytes) -> bytes:
    """换行归一化 \\r\\n → \\n。

    04 §2.1.3：Windows/Linux 混合场景不归一化会误报（P0，见 05 P0-1）。
    """
    return content.replace(b"\r\n", b"\n")


def hash_bytes(content: bytes) -> str:
    """单文件内容哈希（先做换行归一化）。"""
    return hashlib.sha256(normalize_bytes(content)).hexdigest()


def aggregate_hash(files: dict[str, bytes]) -> str:
    """目录级哈希：sha256(path + "\\0" + sha256(content)) 的排序后聚合。

    - 路径统一 POSIX 分隔符（05 P2-3）
    - 内容先换行归一化（\\r\\n → \\n）
    - 排序保证与遍历顺序无关（纯函数前提，NFR-3.5）
    """
    digests = []
    for p in sorted(files):
        posix = p.replace("\\", "/")
        digests.append(f"{posix}\0{hash_bytes(files[p])}")
    return hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 状态判定


def classify(
    adapter_new: bool,
    disk_dirty: bool,
    content_new: bool,
) -> tuple[DriftState, bool]:
    """三方比对判定表（04 §2.1.3 第 2 步）。

    返回 (state, adapter_outdated)。六行覆盖全部 8 种组合，无遗漏、无重叠。

    关键规则（FR-1.0.19）：adapter_new 时不得判 CONFLICT —— local 是「旧 adapter
    渲染 + 用户改动」的混合体，先应用新模板，再单独处理用户改动，两步分开。
    """
    if adapter_new:
        # 磁盘是否被手改决定走 OUTDATED（可整体应用新模板）还是 MODIFIED（需整文件三选一）
        if disk_dirty:
            return DriftState.MODIFIED, True
        return DriftState.OUTDATED, True
    if disk_dirty and content_new:
        return DriftState.CONFLICT, False
    if disk_dirty:
        return DriftState.MODIFIED, False
    if content_new:
        return DriftState.OUTDATED, False
    return DriftState.SYNCED, False


def classify_imported(local_hash: str | None, remote_hash: str | None) -> DriftState:
    """imported 基线的降级判定（04 §2.2.5）：只做两方比对，不进 CONFLICT。

    baseline_source=imported 时磁盘内容是用户写的，无 base 可言，
    无法区分「仓库超前」还是「磁盘超前」，UI 标注「待确认」。
    """
    if local_hash == remote_hash:
        return DriftState.SYNCED
    return DriftState.MODIFIED


def classify_ownership(
    disk_exists: bool,
    binding_exists: bool,
    canonical_exists: bool,
) -> DriftState | None:
    """归属判定（04 §2.1.3 第 1 步，三事实）。

    返回 None 表示「进入第 2 步内容比对」；返回 DriftState 表示终态。
    """
    if not disk_exists and not binding_exists and canonical_exists:
        # 该 runtime 上不适用，不进矩阵 —— 调用方应跳过
        return None
    if disk_exists and not binding_exists:
        return DriftState.UNMANAGED
    if disk_exists and binding_exists and not canonical_exists:
        return DriftState.ORPHANED
    if not disk_exists and binding_exists and canonical_exists:
        return DriftState.MISSING
    if disk_exists and binding_exists and canonical_exists:
        return None  # 进入内容比对
    # disk_exists=False 且 canonical_exists=False：无意义组合，防御性处理为 MISSING
    return DriftState.MISSING
