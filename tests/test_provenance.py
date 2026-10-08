"""溯源单测（FR-1.0.22 / 04 §2.2.3）。

数据源直接用 git log，不另建表（成本低的原因）。rollback 也写溯源 ——
否则账本出现空档（谁把哪个版本退回去了无从追查）。
"""

from __future__ import annotations

from skillos.core.fs.provenance import ProvenanceEvent, provenance_events
from skillos.core.model import Commit


def _commit(message: str, author: str = "you@laptop", sha: str = "abc123") -> Commit:
    return Commit(sha=sha, message=message, author=author, at="2026-10-07T10:00:00")


class TestActions:
    def test_import_action(self) -> None:
        events = provenance_events([_commit("skillos: import brainstorming from 2 runtimes")])
        assert events[0].action == "import"

    def test_distribute_action(self) -> None:
        events = provenance_events([_commit("skillos: distribute code-review to 4 runtimes")])
        assert events[0].action == "distribute"

    def test_rollback_action(self) -> None:
        events = provenance_events([_commit("skillos: rollback skills/demo to abc123")])
        assert events[0].action == "rollback"

    def test_safety_snapshot_action(self) -> None:
        events = provenance_events([_commit("skillos: snapshot before rollback")])
        assert events[0].action == "snapshot"

    def test_plain_commit_is_edit(self) -> None:
        """用户/编辑器产生的普通 commit 归为 edit（时间轴仍显示作者与时间）。"""
        events = provenance_events([_commit("update description")])
        assert events[0].action == "edit"

    def test_multiline_message(self) -> None:
        events = provenance_events(
            [_commit("skillos: distribute x\n\nSkillos-Protocol: 1\n\nfiles changed")]
        )
        assert events[0].action == "distribute"


class TestFields:
    def test_event_fields(self) -> None:
        events = provenance_events([_commit("skillos: import demo", author="alice", sha="ff01")])
        e = events[0]
        assert e.author == "alice"
        assert e.commit == "ff01"
        assert e.at == "2026-10-07T10:00:00"
        assert e.action == "import"

    def test_newest_first_preserved(self) -> None:
        """GitRepo.log 已按新→旧排序；本模块保持顺序（时间轴直接消费）。"""
        commits = [
            _commit("newest", sha="n1"),
            _commit("skillos: import old", sha="n2"),
        ]
        events = provenance_events(commits)
        assert [e.commit for e in events] == ["n1", "n2"]

    def test_empty(self) -> None:
        assert provenance_events([]) == []


def test_event_dataclass() -> None:
    e = ProvenanceEvent(author="a", commit="c", at="t", action="edit", detail="")
    assert e.action == "edit"
