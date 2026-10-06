"""drift 判定单测：三方比对判定表 8 种组合全覆盖（04 §2.1.3）+ 哈希口径。"""

from __future__ import annotations

import pytest

from skillos.core.algorithm.drift import (
    aggregate_hash,
    classify,
    classify_imported,
    classify_ownership,
    hash_bytes,
)
from skillos.core.model import DriftState


class TestClassify:
    """判定表六行（adapter_new 的 2×2 + 其余 4 行）。"""

    def test_all_synced(self):
        assert classify(False, False, False) == (DriftState.SYNCED, False)

    def test_outdated(self):
        # 仓库领先，可直接 push
        assert classify(False, False, True) == (DriftState.OUTDATED, False)

    def test_modified(self):
        # 磁盘被手改
        assert classify(False, True, False) == (DriftState.MODIFIED, False)

    def test_conflict(self):
        # 两边都改，需人工三选一
        assert classify(False, True, True) == (DriftState.CONFLICT, False)

    def test_adapter_new_clean(self):
        # FR-1.0.19：adapter_new 不判 CONFLICT；磁盘干净 → OUTDATED + 模板徽标
        assert classify(True, False, False) == (DriftState.OUTDATED, True)
        assert classify(True, False, True) == (DriftState.OUTDATED, True)

    def test_adapter_new_dirty(self):
        # 磁盘被改 → MODIFIED + 模板徽标，整文件三选一
        assert classify(True, True, False) == (DriftState.MODIFIED, True)
        assert classify(True, True, True) == (DriftState.MODIFIED, True)


class TestImportedBaseline:
    """imported 基线只做两方比对，不进 CONFLICT（04 §2.2.5）。"""

    def test_same(self):
        assert classify_imported("h1", "h1") == DriftState.SYNCED

    def test_diff(self):
        assert classify_imported("h1", "h2") == DriftState.MODIFIED


class TestOwnership:
    def test_not_applicable(self):
        # 磁盘无目录、binding 无记录、canonical 有 → 不进矩阵
        assert classify_ownership(False, False, True) is None

    def test_unmanaged(self):
        assert classify_ownership(True, False, True) == DriftState.UNMANAGED
        assert classify_ownership(True, False, False) == DriftState.UNMANAGED

    def test_orphaned(self):
        assert classify_ownership(True, True, False) == DriftState.ORPHANED

    def test_missing(self):
        assert classify_ownership(False, True, True) == DriftState.MISSING

    def test_enter_content_compare(self):
        assert classify_ownership(True, True, True) is None


class TestHash:
    def test_crlf_normalized(self):
        """04 §2.1.3：换行归一化，Windows/Linux 混合不误报。"""
        assert hash_bytes(b"a\r\nb\r\n") == hash_bytes(b"a\nb\n")

    def test_aggregate_order_independent(self):
        a = aggregate_hash({"x.md": b"1", "y.md": b"2"})
        b = aggregate_hash({"y.md": b"2", "x.md": b"1"})
        assert a == b

    def test_aggregate_posix_separator(self):
        assert aggregate_hash({"a/b.md": b"1"}) == aggregate_hash({"a\\b.md": b"1"})

    def test_aggregate_different_content_differs(self):
        assert aggregate_hash({"x.md": b"1"}) != aggregate_hash({"x.md": b"2"})

    def test_aggregate_pure(self):
        """NFR-3.5 纯函数前提：同输入两次调用逐字节相同。"""
        files = {"SKILL.md": b"---\nname: x\n---\n", "a/b.md": b"hello\r\n"}
        assert aggregate_hash(files) == aggregate_hash(dict(files))


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (DriftState.SYNCED, "synced"),
        (DriftState.UNMANAGED, "unmanaged"),
        (DriftState.ORPHANED, "orphaned"),
    ],
)
def test_state_str_values(state: DriftState, expected: str) -> None:
    """7 态字符串值与 WS / REST 协议一致（T8）。"""
    assert state.value == expected
