"""daemon 本机单实例锁（见 daemon/main.py acquire_instance_lock）。

背景：两个 daemon 实例会互踢（云端 4001 → 1s 重连 → 反踢）成重连风暴，
且每个被拒连接都会在云端认证阶段占住一个池连接。源头锁死后连第二个
实例都不该起来。
"""

from __future__ import annotations

import logging

import pytest

from skillos.daemon.main import acquire_instance_lock


def test_second_instance_exits_first_wins(tmp_path, caplog):
    # 先占锁（模拟已在运行的 daemon 进程）
    first = acquire_instance_lock(tmp_path)
    assert first.is_locked

    # 第二实例：1s 内抢不到 → SystemExit(1)，结构化事件 instance_lock_busy（T23）
    with pytest.raises(SystemExit) as ei, caplog.at_level(logging.ERROR):
        acquire_instance_lock(tmp_path)
    assert ei.value.code == 1
    assert "instance_lock_busy" in caplog.text

    # 旧进程退出释放后，新实例可正常启动
    first.release()
    second = acquire_instance_lock(tmp_path)
    assert second.is_locked
    second.release()


def test_lock_file_lives_under_skillos_dir(tmp_path):
    lock = acquire_instance_lock(tmp_path)
    assert (tmp_path / ".skillos" / "daemon.lock").exists()
    lock.release()
