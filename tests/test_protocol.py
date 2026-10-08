"""仓库协议版本协商单测（04 §2.1.3 / FR-1.0.29 / T18 / 06 A4）。

价值不在单机场景，而在不改数据结构就能扩展到 V2 的多客户端防线：
- trailer 缺失 = 旧客户端写入 → 拒绝混用（PROTOCOL_MISMATCH）
- trailer 版本 > 本机支持 → 只读模式，禁止写入
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skillos.core.fs.protocol import (
    ProtocolCheck,
    ProtocolMismatch,
    append_trailer,
    check,
    parse_trailer,
    read_sticky,
    write_sticky,
)


class TestTrailer:
    def test_append_and_parse(self) -> None:
        msg = append_trailer("feat: add skill")
        assert msg == "feat: add skill\n\nSkillos-Protocol: 1"
        assert parse_trailer(msg) == 1

    def test_append_idempotent(self) -> None:
        once = append_trailer("m")
        twice = append_trailer(once)
        assert once == twice

    def test_parse_missing_returns_none(self) -> None:
        assert parse_trailer("plain message") is None
        assert parse_trailer("") is None


class TestCheck:
    def test_normal_writable(self) -> None:
        res = check(head_message="m\n\nSkillos-Protocol: 1", sticky_version=1, supported=1)
        assert res.ok and not res.read_only

    def test_old_client_missing_trailer_rejected(self) -> None:
        """树里有 protocol.json 但 commit 无 trailer = 旧客户端写入 → 拒绝混用。"""
        res = check(head_message="no trailer here", sticky_version=1, supported=1)
        assert not res.ok
        assert res.reason == "PROTOCOL_MISMATCH"

    def test_future_version_read_only(self) -> None:
        """trailer 版本 > 本机支持 → 只读模式（不拒绝读，禁止写）。"""
        res = check(head_message="m\n\nSkillos-Protocol: 2", sticky_version=2, supported=1)
        assert res.read_only
        assert not res.ok

    def test_no_sticky_no_trailer_ok(self) -> None:
        """全新仓库（无 sticky 无 trailer）：可写，首写时建立 sticky。"""
        res = check(head_message=None, sticky_version=None, supported=1)
        assert res.ok and not res.read_only


class TestSticky:
    def test_write_read_roundtrip(self, tmp_path: Path) -> None:
        write_sticky(tmp_path, 1)
        assert read_sticky(tmp_path) == 1

    def test_only_upgrade_never_downgrade(self, tmp_path: Path) -> None:
        """sticky 只升不降（04 §2.1.3）。"""
        write_sticky(tmp_path, 2)
        with pytest.raises(ProtocolMismatch):
            write_sticky(tmp_path, 1)
        assert read_sticky(tmp_path) == 2

    def test_corrupted_sticky_returns_none(self, tmp_path: Path) -> None:
        """损坏 → 返回 None（不猜版本），调用方可重写。"""
        from skillos.core.fs.meta import META_DIR

        d = tmp_path / META_DIR
        d.mkdir()
        (d / "protocol.json").write_text("{corrupt", encoding="utf-8")
        assert read_sticky(tmp_path) is None

    def test_missing_sticky_returns_none(self, tmp_path: Path) -> None:
        assert read_sticky(tmp_path) is None


def test_protocol_check_dataclass() -> None:
    res = ProtocolCheck(ok=True, read_only=False, reason=None)
    assert res.ok and res.reason is None
