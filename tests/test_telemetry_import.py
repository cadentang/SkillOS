"""V2-M2-06：L2 import 编排（N-11.4 / N-11.9，docs/12 §5.4）。

重点验三条规格约束：
- 路径优先级：手动 > 自动探测，手动存在时**跳过**探测（N-11.9③）；
- 不静默产空：路径/文件找不到 → `TelemetryPathError` 且带尝试清单（N-11.9④）；
- token 落事件行：aggregate 重算两次，token 不丢（TQ-02 的直接后果）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from skillos.core.algorithm.telemetry_parser import load_spec
from skillos.services.telemetry_service import (
    L2_OP,
    L2_VIA,
    TelemetryPathError,
    TelemetryService,
    collect_session_files,
    resolve_paths,
)
from skillos.services.usage_service import UsageService
from skillos.store.db import init_schema, make_engine, make_session_factory
from skillos.store.index_repo import SqliteIndexStore
from skillos.store.models import SkillUsageDaily, SkillUsageEvent


class _Settings:
    def __init__(self, paths: dict[str, list[str]] | None = None) -> None:
        self._paths = paths or {}

    def runtime_paths(self) -> dict[str, list[str]]:
        return self._paths


class _SwitchOn:
    def collect_enabled(self) -> bool:
        return True


def _store() -> SqliteIndexStore:
    engine = make_engine("sqlite://")
    init_schema(engine)
    return SqliteIndexStore(make_session_factory(engine)())


def _session_line(ts: str, *, content: str = "", tokens: int = 10) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "content": content,
            "usage": {"input_tokens": tokens, "output_tokens": tokens // 2},
        },
    }, ensure_ascii=False)


def _daily(store: SqliteIndexStore) -> list[tuple[str, int, int]]:
    return [
        (r.skill_id, r.tokens_in, r.tokens_out)
        for r in store._s.scalars(select(SkillUsageDaily))
    ]


@pytest.fixture()
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    deploy_root = tmp_path / "runtimes" / "claude" / "skills"
    deploy_root.mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "a.jsonl").write_text(
        "\n".join([
            _session_line("2026-10-24T09:00:05Z",
                          content=f"{deploy_root}/alpha/SKILL.md", tokens=20),
            _session_line("2026-10-24T09:00:08Z", tokens=6),
        ]) + "\n",
        encoding="utf-8",
    )
    (sessions / "b.jsonl").write_text(
        "{ broken json\n" + _session_line("2026-10-24T09:05:00Z", tokens=4) + "\n",
        encoding="utf-8",
    )
    return deploy_root, sessions


class TestPathResolution:
    def test_manual_paths_skip_autodetect(self, tmp_path: Path) -> None:
        spec = load_spec("claude-code")
        paths, manual = resolve_paths(spec, _Settings({"claude-code": [str(tmp_path)]}), tmp_path)
        assert manual is True
        assert paths == [tmp_path]

    def test_autodetect_when_no_manual(self, tmp_path: Path) -> None:
        spec = load_spec("claude-code")
        paths, manual = resolve_paths(spec, _Settings(), tmp_path)
        assert manual is False
        assert str(paths[0]).replace("\\", "/").endswith(".claude/projects")

    def test_all_paths_missing_raises_with_attempt_list(self, tmp_path: Path) -> None:
        spec = load_spec("claude-code")
        missing = tmp_path / "nope"
        with pytest.raises(TelemetryPathError) as exc:
            collect_session_files([missing], spec)
        assert str(missing) in str(exc.value)
        assert "telemetry path set" in str(exc.value)

    def test_empty_dir_raises_instead_of_empty_result(self, tmp_path: Path) -> None:
        """N-11.9④：目录在但没有 *.jsonl ⇒ 报错，不许产空聚合。"""
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(TelemetryPathError):
            collect_session_files([empty], load_spec("claude-code"))

    def test_same_file_via_two_roots_counted_once(self, tmp_path: Path) -> None:
        """N-11.9⑤：多路径指向同一目录时，同一 session 文件只解析一次。"""
        sessions = tmp_path / "s"
        sessions.mkdir()
        (sessions / "x.jsonl").write_text(
            _session_line("2026-10-24T09:00:00Z") + "\n", encoding="utf-8",
        )
        files = collect_session_files([sessions, sessions / "."], load_spec("claude-code"))
        assert len(files) == 1


class TestImport:
    def _service(self, store: SqliteIndexStore, sessions: Path, deploy_roots) -> TelemetryService:
        return TelemetryService(
            store=store, settings=_Settings({"claude-code": [str(sessions)]}),
            home=sessions, deploy_roots=deploy_roots,
        )

    def test_import_attributes_tokens_to_skill(self, workspace) -> None:
        deploy_root, sessions = workspace
        store = _store()
        result = self._service(store, sessions, [deploy_root]).import_runtime("claude-code")

        assert result.files == 2
        assert result.skipped_lines == 1                       # 坏行计数，不中断
        assert result.by_skill["alpha"]["tokens_in"] == 30      # 20 + 6 + 4 都归 alpha
        assert result.events_written == 1

        rows = list(store._s.scalars(select(SkillUsageEvent)))
        assert [(r.op, r.via, r.skill_id, r.tokens_in) for r in rows] == [
            (L2_OP, L2_VIA, "alpha", 30)
        ]

    def test_tokens_survive_aggregate_recompute(self, workspace) -> None:
        """TQ-02：token 记在事件行上，所以「重算两次」不会把它们抹掉。"""
        deploy_root, sessions = workspace
        store = _store()
        self._service(store, sessions, [deploy_root]).import_runtime("claude-code")

        usage = UsageService(store, _SwitchOn())
        now = datetime.now(UTC)
        usage.aggregate(now=now)
        first = _daily(store)
        usage.aggregate(now=now)
        assert _daily(store) == first
        assert first == [("alpha", 30, 15)]

    def test_unknown_runtime_raises_path_error(self, tmp_path: Path) -> None:
        svc = TelemetryService(store=_store(), settings=_Settings(), home=tmp_path)
        with pytest.raises(TelemetryPathError):
            svc.import_runtime("no-such-runtime")

    def test_unattributed_tokens_reported_not_forced(self, workspace) -> None:
        _, sessions = workspace
        store = _store()
        # 不给 deploy_root：绝对路径不再命中，全部进 unattributed（不强行归属）
        result = self._service(store, sessions, []).import_runtime("claude-code")
        assert result.by_skill == {}
        assert result.unattributed["tokens_in"] == 30
        assert result.events_written == 0
