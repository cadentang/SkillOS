"""M2 里程碑集成（V2-M2-10）：真实 Claude Code JSONL 走完一整条链路。

链路：JSONL → L2 解析 → skill 归属 → 事件落库 → 日聚合（token 进 daily）
→ 上报关闭态零报文 → 开启后报文含 token → 体检报告落库。

为什么必须用真 engine + 真磁盘：这条链路的失败方式几乎都是"每段单独看都对，
拼起来不通"（比如 token 只写日聚合、下一次重算就被抹掉 —— TQ-02 的坑）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from skillos.services.engine import EnvPaths, LocalEngine
from skillos.services.telemetry_service import TelemetryService
from skillos.store.models import SkillUsageEvent

_SKILL_MD = (
    b"---\nname: alpha\nversion: 1.0.0\n"
    b"description: A demo skill used by the M2 integration flow test.\n---\nbody\n"
)


def _line(ts: datetime, *, content: str = "", tokens_in: int = 0,
          tokens_out: int = 0) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "message": {
            "content": content,
            "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        },
    }, ensure_ascii=False)


def _engine(tmp_path: Path) -> tuple[LocalEngine, Path]:
    home = tmp_path / "home"
    deploy = home / ".claude" / "skills" / "alpha"
    deploy.mkdir(parents=True)
    (deploy / "SKILL.md").write_bytes(_SKILL_MD)
    engine = LocalEngine(
        EnvPaths(repo_root=tmp_path / "repo", db_path=tmp_path / "state" / "s.db"),
        home=home,
    )
    engine.init_env()
    engine.import_skills()
    return engine, home / ".claude" / "skills"


def _write_session(sessions: Path, deploy_root: Path) -> None:
    sessions.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).replace(tzinfo=None)
    lines = [
        # 首个调用事件之前：没有任何 skill 参与 ⇒ 归 unattributed（N-11.3）
        _line(now - timedelta(minutes=3),
              content="/tmp/unrelated/file.md", tokens_in=999, tokens_out=999),
        # 命中部署目录 → 归属 alpha；带 token。
        # 用 POSIX 形式：Claude Code 由 Node 写日志，路径一律 `/`（Windows 上也是）
        _line(now - timedelta(minutes=2),
              content=(deploy_root / "alpha" / "SKILL.md").as_posix(),
              tokens_in=120, tokens_out=45),
        # 同会话后续行仍归 alpha（时间片归属：最后一次调用之后的 usage 也算它的）
        _line(now - timedelta(minutes=1), tokens_in=30, tokens_out=10),
    ]
    (sessions / "s1.jsonl").write_text(
        "{ broken json line\n" + "\n".join(lines) + "\n", encoding="utf-8",
    )


class TestM2Flow:
    def test_jsonl_to_daily_to_report(self, tmp_path: Path) -> None:
        engine, deploy_root = _engine(tmp_path)
        sessions = tmp_path / "sessions"
        _write_session(sessions, deploy_root)
        engine.settings.set("telemetry.runtime_paths", {"claude-code": [str(sessions)]})

        # ① L2 导入：坏行计数、token 归属 alpha
        result = TelemetryService(
            store=engine.db, settings=engine.settings,
            home=tmp_path / "home", deploy_roots=[deploy_root],
        ).import_runtime("claude-code")
        assert result.skipped_lines == 1
        assert result.by_skill["alpha"]["tokens_in"] == 150      # 120 + 30（同会话后续）
        assert result.unattributed["tokens_in"] == 999           # 首个调用之前的花费

        events = list(engine.db._s.query(SkillUsageEvent))
        assert [e.skill_id for e in events] == ["alpha"]         # 未命中不归属
        assert events[0].tokens_in == 150

        # ② 日聚合：token 必须进 daily（否则上报与团队视图都看不到）
        engine.usage.aggregate()
        daily = engine.usage.totals_by_skill(days=30)
        assert daily["alpha"]["tokens_in"] == 150
        assert daily["alpha"]["tokens_out"] == 55

        # ③ 上报关闭态：默认 off ⇒ 零报文（N-01.5 / NFR-V2-03）
        assert engine.usage.report_rows() == []

        # ④ 开启后报文含 token，且只有聚合字段
        engine.settings.set("usage_report_enabled", True)
        rows = engine.usage.report_rows()
        assert rows and rows[0]["tokens_in"] == 150
        assert rows[0]["skill_id"] == "alpha"

        # ⑤ 体检报告落库（N-02）：无变化也要留痕
        first = engine.reports.generate()
        second = engine.reports.generate()
        assert first["id"] and second["id"] and first["id"] != second["id"]
        assert len(engine.reports.list_reports(limit=10)) == 2
        assert engine.reports.get_report(first["id"]) is not None

        # ⑥ 管理者视图口径：alpha 有调用 ⇒ 不在零调用榜
        assert engine.usage.zero_called(slugs=engine.all_slugs(), days=30) == []
