"""L2 会话日志导入编排（N-11.4 / N-11.9，`docs/12` §5.4）。

本层只做「找文件 → 流式读 → 去重 → 调纯函数 → 落事件表」，解析规则全在
`core/algorithm/telemetry_parser.py` + `adapters/telemetry/*.yaml` 里。

三条规格硬约束：
- **路径解析优先级**：手动指定（`telemetry.runtime_paths`）> 自动探测；手动存在时**跳过探测**
  （N-11.9③），多路径逐目录扫描、互不覆盖；
- **不静默产空结果**：路径不存在 / 目录内无匹配文件 → 抛 `TelemetryPathError` 并附尝试清单
  （N-11.9④）；
- **token 是估算**（N-11.3）：落库时按时间片归属写进事件行，UI 必须标注估算口径。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from skillos.core.algorithm.telemetry import Attribution, attribute_tokens
from skillos.core.algorithm.telemetry_parser import (
    AdapterSpec,
    TelemetryAdapterError,
    expand_autodetect,
    load_spec,
    parse_lines,
)

# L2 事件在事件表里的标签：与 L1 的 mcp/web/cli/daemon 区分，展示层可单独过滤
L2_OP = "session_usage"
L2_VIA = "daemon"


class TelemetryPathError(Exception):
    """N-11.9④：找不到可解析的会话文件。**必须**带上尝试过的路径清单。"""

    def __init__(self, runtime: str, attempted: Sequence[Path], reason: str) -> None:
        self.runtime = runtime
        self.attempted = list(attempted)
        self.reason = reason
        lines = "\n".join(f"  - {p}" for p in self.attempted) or "  (无)"
        super().__init__(
            f"{runtime}: {reason}\n已尝试路径:\n{lines}\n"
            f"可用 `skillos telemetry path set {runtime} <dir...>` 手动指定（N-11.9）"
        )


class _Store(Protocol):
    def add_usage_event(self, *, session_id: str, runtime: str, skill_id: str, op: str,
                        via: str, duration_ms: int | None, ok: bool,
                        param_digest: str | None,
                        tokens_in: int = 0, tokens_out: int = 0) -> None: ...


class _Settings(Protocol):
    def runtime_paths(self) -> dict[str, list[str]]: ...


@dataclass
class ImportResult:
    """导入结果（给 CLI / Web 展示；不含正文，N-11.8）。"""

    runtime: str
    paths: list[str] = field(default_factory=list)
    files: int = 0
    total_lines: int = 0
    skipped_lines: int = 0
    usage_lines: int = 0
    duplicate_files: int = 0
    events_written: int = 0
    by_skill: dict[str, dict[str, int]] = field(default_factory=dict)
    unattributed: dict[str, int] = field(default_factory=dict)

    @property
    def token_total(self) -> int:
        return sum(v["tokens_in"] + v["tokens_out"] for v in self.by_skill.values())


def resolve_paths(
    spec: AdapterSpec,
    settings: _Settings,
    home: Path,
) -> tuple[list[Path], bool]:
    """返回 (路径集, 是否手动)。N-11.9③：手动存在时整段跳过自动探测。"""
    manual = settings.runtime_paths().get(spec.runtime) or []
    if manual:
        return [Path(p).expanduser() for p in manual], True
    return expand_autodetect(spec, home), False


def collect_session_files(paths: Sequence[Path], spec: AdapterSpec) -> list[Path]:
    """逐路径展开 `session_glob`，按真实路径去重（多根指向同一文件只算一次，N-11.9⑤）。"""
    missing = [p for p in paths if not p.is_dir()]
    if missing and len(missing) == len(paths):
        raise TelemetryPathError(spec.runtime, paths, "配置的路径全部不存在或不是目录")

    found: dict[str, Path] = {}
    for root in paths:
        if not root.is_dir():
            continue
        for file in sorted(root.glob(spec.session_glob)):
            if file.is_file():
                found.setdefault(os.path.normcase(str(file.resolve())), file)
    if not found:
        raise TelemetryPathError(
            spec.runtime, paths,
            f"目录里没有匹配 {spec.session_glob} 的会话文件",
        )
    return list(found.values())


class TelemetryService:
    """L2 导入：手动优先的路径解析 + 流式解析 + 时间片归属 + 事件表落库。"""

    def __init__(self, *, store: _Store, settings: _Settings, home: Path,
                 deploy_roots: Iterable[Path] = ()) -> None:
        self._store = store
        self._settings = settings
        self._home = home
        self._deploy_roots = tuple(deploy_roots)

    def import_runtime(self, runtime_key: str) -> ImportResult:
        try:
            spec = load_spec(runtime_key)
        except TelemetryAdapterError as exc:      # 配置缺失也要报错，不产空结果
            raise TelemetryPathError(runtime_key, [], str(exc)) from exc

        paths, _manual = resolve_paths(spec, self._settings, self._home)
        files = collect_session_files(paths, spec)
        result = ImportResult(runtime=runtime_key, paths=[str(p) for p in paths])
        result.files = len(files)

        calls = []
        slices = []
        for file in files:
            parsed = self._parse_file(file, spec)
            calls.extend(parsed.calls)
            slices.extend(parsed.slices)
            result.total_lines += parsed.total_lines
            result.skipped_lines += parsed.skipped_lines
            result.usage_lines += parsed.usage_lines

        attribution = attribute_tokens(calls, slices)
        self._persist(attribution, result)
        return result

    def _parse_file(self, file: Path, spec: AdapterSpec):
        # 流式：文件对象逐行喂给解析器，不整体 read()（会话 JSONL 可达数百 MB）
        with file.open("r", encoding="utf-8", errors="replace") as fh:
            return parse_lines(fh, spec, deploy_roots=self._deploy_roots)

    def _persist(self, attribution: Attribution, result: ImportResult) -> None:
        """归属结果落事件表：一个 skill 一条，token 记在事件行上供聚合重算。"""
        for skill_id, totals in attribution.by_skill.items():
            self._store.add_usage_event(
                session_id="l2_import",
                runtime=result.runtime,
                skill_id=skill_id,
                op=L2_OP,
                via=L2_VIA,
                duration_ms=None,
                ok=True,
                param_digest=None,
                tokens_in=totals.input_tokens,
                tokens_out=totals.output_tokens,
            )
            result.events_written += 1
            result.by_skill[skill_id] = totals.as_dict()
        result.unattributed = attribution.unattributed.as_dict()
