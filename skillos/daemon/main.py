"""daemon 主循环（FR-1.2.x，04 §2.5.2）。

出站 WS 连云端：
- 握手：优先长期 token；无 token 时走一次性配对码（--pair）
- 任务执行器：**领域操作白名单**（WS_TASK_OPS），无 request_id 直接拒绝，
  写操作持全局写锁（FR-1.2.4）、request_id 近窗口 LRU 去重（07 N12 守卫②③）
- 心跳保活 + 断线指数退避重连（1s,2s,4s,…,60s 上限，FR-1.2.5）
- 连接建立后上报一次漂移矩阵；push 后与周期性再报（FR-1.2.6）

启动顺序：环境校验（WSL 边界 / 云同步目录）→ 连接 → 注册 → 循环。
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import socket
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

import websockets
from filelock import FileLock, Timeout

from skillos.core.constants import LOCK_TIMEOUT_S, WS_BACKOFF_MAX_S, WS_TASK_OPS
from skillos.core.logging import (
    bind_op_id,
    get_logger,
    reset_op_context,
    setup_logging,
)
from skillos.services.engine import (
    LocalEngine,
    SkillValidationError,
    all_adapters,
    default_env,
    resolve_home,
)
from skillos.services.locking import write_lock

log = get_logger("skillos.daemon")

HEARTBEAT_S = 20
REPORT_INTERVAL_S = 60
_REQ_LRU_SIZE = 512


def _parse_log_json(line: str) -> dict | None:
    """解析一行 JSON 日志。key=value 形态暂不解析（云端走 JSON 渲染）。

    返回的 dict 含 ts / logger / level / event / op_id 等核心字段；
    其余字段（如 duration_ms / path / error_code）原样带过去。
    """
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    import json as _json

    try:
        rec = _json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(rec, dict):
        return None
    return rec


class RequestDedup:
    """request_id 近窗口去重（07 N12 守卫②）：重连补发 / 网络重试防双执行。"""

    def __init__(self, size: int = _REQ_LRU_SIZE) -> None:
        self._seen: OrderedDict[str, str] = OrderedDict()  # request_id → task_id
        self._size = size

    def seen(self, request_id: str) -> bool:
        return request_id in self._seen

    def mark(self, request_id: str, task_id: str) -> None:
        self._seen[request_id] = task_id
        while len(self._seen) > self._size:
            self._seen.popitem(last=False)


class TaskExecutor:
    """白名单任务执行器。写类 op 持全局写锁（FR-1.2.4）。

    push 的锁在 services.engine.push_skill 内部（三入口共享同一入口锁），
    此处不得再套一层 —— 同路径两个 FileLock 实例在 Windows 上互斥，会死锁。
    """

    # 引擎内部不带锁的写操作（git 直调类）。
    # 注意：engine.push_skill / write_file / rollback_skill / pull_skill 内部
    # 已自加写锁（FR-1.0.17），这里再加会形成同进程双 FileLock 自锁
    # （msvcrt 层互斥 → 30s 死锁，实测踩坑）。tag_create 直接调 git（无引擎锁）→ 此处加。
    WRITE_OPS = frozenset({"tag_create"})

    def __init__(self, engine: LocalEngine) -> None:
        self.engine = engine
        self.dedup = RequestDedup()

    def execute(self, op: str, params: dict) -> dict:
        """返回 result 载荷（{"ok":..., "data"/"error"}）。异常不外抛。"""
        if op not in WS_TASK_OPS:
            return {"ok": False, "error": {"code": "INVALID_INPUT",
                                           "message": f"非白名单操作: {op}"}}
        try:
            if op in self.WRITE_OPS:
                with write_lock(self.engine.paths.lock_dir, timeout=LOCK_TIMEOUT_S):
                    return self._dispatch(op, params)
            return self._dispatch(op, params)
        except Exception as e:  # noqa: BLE001 —— result 信封承载错误，不让 daemon 崩
            log.exception("task %s failed", op)
            return {"ok": False, "error": {"code": "IO", "message": str(e)}}

    def _dispatch(self, op: str, params: dict) -> dict:
        engine = self.engine
        if op == "scan":
            items = [
                {
                    "runtime_key": f.runtime_key,
                    "rel_path": f.rel_path,
                    "name": f.meta.name,
                }
                for f in engine.scan_all()
            ]
            return {"ok": True, "data": {"items": items}}
        if op == "status":
            return {"ok": True, "data": {"matrix": _matrix_payload(engine)}}
        if op == "gate_summary":
            return self._gate_summary()
        if op == "push":
            return self._push(params)
        if op == "version_log":
            slug = str(params.get("slug") or "")
            commits = engine.git.log(path=f"skills/{slug}",
                                     limit=int(params.get("limit") or 20))
            # author 必带（FR-2.2.7：谁在什么时候改了什么）
            return {"ok": True, "data": {"items": [
                {"sha": c.sha, "at": c.at, "message": c.message,
                 "author": c.author} for c in commits
            ]}}
        if op == "diff":
            slug = str(params.get("slug") or "")
            base = engine.git.resolve_ref(params.get("base") or "HEAD")
            head = engine.git.resolve_ref(params.get("head") or "HEAD")
            return {"ok": True, "data": {
                "unified": engine.git.diff(base, head, path=f"skills/{slug}"),
            }}
        if op == "file_tree":
            slug = str(params.get("slug") or "")
            return {"ok": True, "data": {"tree": engine.file_tree(slug)}}
        if op == "file_write":
            return self._file_write(params)
        if op == "check":
            return self._check(params)
        if op == "decay":
            slug = str(params.get("slug") or "")
            return {"ok": True, "data": engine.decay_metrics(slug)}
        if op == "rollback":
            # P4：走引擎编排（removals 预览/确认 FR-1.0.26 + 溯源 FR-1.0.22 + NFR-9）
            try:
                result = self.engine.rollback_skill(
                    str(params.get("slug") or ""),
                    str(params.get("ref") or ""),
                    dry_run=bool(params.get("dry_run", True)),
                    confirmed_removals=[str(p) for p in (params.get("confirm_removals") or [])],
                    author=str(params.get("author") or "web <web@skillos.local>"),
                )
            except ValueError as e:
                return {"ok": False, "error": {"code": "INVALID_INPUT", "message": str(e)}}
            if result.get("ok"):
                return {"ok": True, "data": result}
            return {"ok": False, "error": {
                "code": result.get("error_code", "IO"),
                "message": result.get("detail", ""),
            }, "removals": result.get("removals", [])}
        if op == "file_history":
            try:
                items = self.engine.file_history(
                    str(params.get("slug") or ""), str(params.get("path") or ""),
                    limit=int(params.get("limit") or 50),
                )
            except ValueError as e:
                return {"ok": False, "error": {"code": "INVALID_INPUT", "message": str(e)}}
            return {"ok": True, "data": {"items": items}}
        if op == "tag_list":
            return {"ok": True, "data": {"items": self.engine.list_tags()}}
        if op == "tag_create":
            try:
                result = self.engine.create_tag(
                    str(params.get("name") or ""), str(params.get("message") or ""),
                )
            except ValueError as e:
                return {"ok": False, "error": {"code": "INVALID_INPUT", "message": str(e)}}
            return {"ok": True, "data": result}
        if op == "resolve_conflict":
            # P3：CONFLICT 四选一（FR-2.2.6 / T22）。engine.resolve_conflict 自加写锁
            # （与 push/pull/rollback 同模式），executor 不再加（见 WRITE_OPS 注释）。
            try:
                result = self.engine.resolve_conflict(
                    str(params.get("slug") or ""),
                    str(params.get("runtime_key") or params.get("runtime") or ""),
                    str(params.get("decision") or ""),
                    confirmed_removals=list(params.get("confirmed_removals") or []),
                    author=str(params.get("author") or "web <web@skillos.local>"),
                )
            except ValueError as e:
                return {"ok": False, "error": {"code": "INVALID_INPUT", "message": str(e)}}
            return {"ok": True, "data": result}
        if op == "file_read":
            return self._file_read(params)
        if op == "pull":
            # P5 回收链路（FR-2.2.6）：engine.pull_skill 内部自加写锁
            # （executor 不得再包一层，见 WRITE_OPS 注释）
            try:
                result = self.engine.pull_skill(
                    str(params.get("slug") or ""),
                    str(params.get("runtime") or ""),
                    dry_run=bool(params.get("dry_run", True)),
                    by="daemon",
                )
            except ValueError as e:
                return {"ok": False, "error": {"code": "INVALID_INPUT", "message": str(e)}}
            if result.get("ok"):
                return {"ok": True, "data": result}
            return {"ok": False, "error": {
                "code": result.get("error_code", "IO"),
                "message": result.get("detail", ""),
            }}
        if op == "skill_create":
            # P3 CRUD（FR-2.1.7）：engine.create_skill 内部自加写锁
            # （executor 不得再包一层，见 WRITE_OPS 注释）
            try:
                result = self.engine.create_skill(
                    str(params.get("slug") or ""),
                    name=str(params.get("name") or ""),
                    description=str(params.get("description") or ""),
                    author=str(params.get("author") or "web <web@skillos.local>"),
                )
            except SkillValidationError as e:
                return {"ok": False, "error": {"code": e.code, "message": str(e)}}
            return {"ok": True, "data": result}
        if op == "skill_delete":
            # P3 CRUD：removals 守卫（FR-1.0.26 ②）。engine.delete_skill 内部
            # 自加写锁 + 锁内重算；executor 不再包一层（见 WRITE_OPS 注释）。
            try:
                result = self.engine.delete_skill(
                    str(params.get("slug") or ""),
                    dry_run=bool(params.get("dry_run", True)),
                    confirmed_removals=[str(p) for p in (params.get("confirm_removals") or [])],
                    author=str(params.get("author") or "web <web@skillos.local>"),
                )
            except SkillValidationError as e:
                return {"ok": False, "error": {"code": e.code, "message": str(e)}}
            if result.get("ok"):
                return {"ok": True, "data": result}
            return {"ok": False, "error": {
                "code": result.get("error_code", "IO"),
                "message": result.get("detail", ""),
            }, "removals": result.get("removals", [])}
        if op == "sandbox_reset":
            # P7（FR-3.10）：恢复预置演示数据。engine.sandbox_reset 内部
            # 自加写锁（文件恢复段）+ 锁外 import/push 各自持锁，executor
            # 不再包一层（见 WRITE_OPS 注释）。非沙盒 engine → NOT_SANDBOX。
            try:
                result = self.engine.sandbox_reset()
            except SkillValidationError as e:
                return {"ok": False, "error": {"code": e.code, "message": str(e)}}
            return {"ok": True, "data": result}
        if op == "logs":
            # P5 日志查看（FR-1.1.12）：具名领域操作，不暴露任意文件读取
            return self._logs(params)
        if op == "runtimes_list":
            # P3 Settings 页（FR-2.4.10 / T26 / FR-1.0.32）：白名单探测 + 4 builtin
            return {"ok": True, "data": engine.list_potential_runtimes()}
        if op == "runtime_diagnose":
            # FR-1.0.33：Runtime 路径诊断（只读，不动磁盘）。
            # custom：云端查表传 root_path；builtin / 白名单候选：传 key，
            # daemon 端按 adapter / yaml 约定路径解析（云端不知道用户 home）。
            try:
                root_path = str(params.get("root_path") or "")
                key = str(params.get("key") or "")
                if root_path:
                    data = engine.diagnose_custom_path(root_path)
                elif key:
                    data = engine.diagnose_runtime_key(key)
                else:
                    return {"ok": False, "error": {
                        "code": "INVALID_INPUT",
                        "message": "runtime_diagnose 需要 root_path 或 key",
                    }}
                return {"ok": True, "data": data}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": {"code": "IO", "message": str(e)}}
        if op == "runtime_set_enabled":
            # FR-2.4.10：builtin 停用/启用（写本地 SQLite runtimes.enabled）。
            # 写操作但触发权完全在用户（Web 按钮 → 云端 handler → 本 task），
            # 与 T25/T26 红线一致（MCP / 后台批量无调用点）。
            try:
                result = engine.set_runtime_enabled(
                    str(params.get("key") or ""),
                    bool(params.get("enabled", True)),
                )
                return {"ok": True, "data": result}
            except ValueError as e:
                return {"ok": False, "error": {"code": "INVALID_INPUT",
                                               "message": str(e)}}

        if op == "runtime_sync_custom":
            # FR-1.0.34 / T25：云端 custom Runtime 全量对齐本地（写 SQLite）。
            # 云端是事实源；discover_only 条目云端不下发（T25 红线）。
            # 触发权在用户（Web 建/改/删 custom → 云端 handler → 本 task）。
            try:
                result = engine.sync_custom_runtimes(
                    list(params.get("items") or []),
                )
                return {"ok": True, "data": result}
            except ValueError as e:
                return {"ok": False, "error": {"code": "INVALID_INPUT",
                                               "message": str(e)}}

        if op == "protocol_status":
            # FR-1.0.29 / T18：sticky + HEAD trailer + 一致性
            return {"ok": True, "data": engine.protocol_status()}
        return {"ok": False, "error": {"code": "INVALID_INPUT",
                                       "message": f"未知操作: {op}"}}

    def _push(self, params: dict) -> dict:
        slugs = [str(s) for s in (params.get("skill_ids") or [])]
        if not slugs:
            return {"ok": False, "error": {"code": "INVALID_INPUT",
                                           "message": "skill_ids 为空"}}
        runtime_keys = params.get("runtime_keys")
        # Web 人工路径确认后的删除清单（FR-1.0.26 / FR-2.4.7）；无人值守路径为空 → held_back
        confirmed = frozenset(str(p) for p in (params.get("confirm_removals") or []))
        results = []
        held_back: list[str] = []
        all_ok = True
        for slug in slugs:
            try:
                report = self.engine.push_skill(
                    slug,
                    runtime_keys=[str(k) for k in runtime_keys] if runtime_keys else None,
                    dry_run=bool(params.get("dry_run")),
                    allow_gate_failure=False,  # MCP/daemon 路径无开关（FR-1.3.6）
                    confirmed_removals=confirmed or None,
                )
            except ValueError as e:
                # 未纳管 / 无 canonical 等：该 Skill 失败，不阻塞其余（FR-2.1.4）
                results.append({"skill": slug, "runtime": None, "ok": False,
                                "error": {"code": "INVALID_INPUT", "message": str(e)}})
                all_ok = False
                continue
            all_ok = all_ok and report.ok
            for o in report.outcomes:
                item = {"skill": slug, "runtime": o.runtime_key, "ok": o.ok}
                if o.error_code:
                    item["error"] = {"code": o.error_code, "message": o.detail or ""}
                    item["held_back_removals"] = list(o.held_back_removals or [])
                    held_back.extend(o.held_back_removals or [])
                results.append(item)
        return {
            "ok": all_ok,
            "data": {"results": results, "held_back_removals": held_back},
            **({} if all_ok else {"error": {
                "code": self._batch_error_code(results, held_back),
                "message": "存在失败目标（其余已照常分发，FR-2.1.4）",
                "held_back_removals": held_back,
            }}),
        }

    @staticmethod
    def _batch_error_code(results: list[dict], held_back: list[str]) -> str:
        """错误信封 code 与 CLI 同源（FR-3.19）：删除待确认优先于其它失败。"""
        if held_back:
            return "REMOVALS_REQUIRE_CONFIRM"
        for item in results:
            err = item.get("error")
            if err:
                return str(err.get("code") or "PUSH_FAILED")
        return "PUSH_FAILED"

    def _gate_summary(self) -> dict:
        """ManagerPanel 门禁计数（FR-2.1.1 决策者读法）：每 skill 的 error / warn 数。

        用 gate_all 批量入口：逐 skill gate_of 是 100+ 次 git spawn（20s+），
        批量 2 次 spawn（亚秒）。
        """
        from skillos.core.algorithm.checks import CheckLevel

        summary: dict[str, dict[str, int]] = {}
        for slug, result in self.engine.gate_all().items():
            errors = sum(1 for f in result.findings if f.level == CheckLevel.ERROR)
            warns = sum(1 for f in result.findings if f.level == CheckLevel.WARN)
            summary[slug] = {"errors": errors, "warns": warns}
        return {"ok": True, "data": {"summary": summary}}

    def _file_read(self, params: dict) -> dict:
        slug = str(params.get("slug") or "")
        rel = str(params.get("path") or "")
        from skillos.core.fs.safe_path import safe_path

        target = safe_path(self.engine.paths.repo_root / "skills" / slug, rel)
        content = target.read_bytes()
        import hashlib

        return {"ok": True, "data": {
            "content": content.decode("utf-8", errors="replace"),
            "hash": hashlib.sha256(content).hexdigest(),
        }}

    def _file_write(self, params: dict) -> dict:
        """编辑器保存（FR-2.4.5 / FR-3.13）：STALE / PATH_TRAVERSAL 结构化错误。"""
        from skillos.core.fs.safe_path import PathTraversalError
        from skillos.services.engine import StaleWriteError

        try:
            result = self.engine.write_file(
                str(params.get("slug") or ""),
                str(params.get("path") or ""),
                str(params.get("content") or "").encode("utf-8"),
                base_hash=str(params.get("base_hash") or ""),
                message=str(params.get("message") or "web 编辑"),
            )
        except StaleWriteError as e:
            return {"ok": False, "error": {"code": "STALE", "message": str(e)}}
        except PathTraversalError as e:
            return {"ok": False, "error": {"code": "PATH_TRAVERSAL", "message": str(e)}}
        return {"ok": True, "data": result}

    def _check(self, params: dict) -> dict:
        """逐条门禁 findings（FR-1.0.11 / NFR-10.5：与 push 共用同一判定入口）。"""
        slug = str(params.get("slug") or "")
        try:
            result = self.engine.gate_of(slug)
        except Exception as e:  # noqa: BLE001 —— canonical 缺失等场景转 INVALID_INPUT
            return {"ok": False, "error": {"code": "INVALID_INPUT", "message": str(e)}}
        return {"ok": True, "data": {
            "has_error": result.has_error,
            "findings": [
                {"rule": f.rule, "level": str(f.level), "message": f.message}
                for f in result.findings
            ],
        }}

    def _logs(self, params: dict) -> dict:
        """读取本地日志（FR-1.1.12）。

        路径固定为 `~/.skillos/logs/<mode>.log`，不允许任意路径（避免任意文件读取）；
        mode 白名单化（与 ws/api 路由一致）；tail 上限 5000。
        """
        mode = str(params.get("mode") or "daemon")
        if mode not in {"daemon", "api", "mcp"}:
            return {"ok": False, "error": {
                "code": "INVALID_INPUT", "message": f"未知 mode: {mode}",
            }}
        try:
            tail = max(0, min(int(params.get("tail") or 100), 5000))
        except (TypeError, ValueError):
            tail = 100
        min_level = str(params.get("level") or "DEBUG").upper()
        op_filter = str(params.get("op_id") or "") or None

        log_path = self.engine.home / ".skillos" / "logs" / f"{mode}.log"
        if not log_path.exists():
            return {"ok": True, "data": {
                "events": [], "skipped": 0, "path": str(log_path),
                "missing": True,
            }}
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return {"ok": False, "error": {
                "code": "IO", "message": f"读日志失败: {e}",
            }}
        lines = content.splitlines()
        if tail > 0:
            lines = lines[-tail:]
        events: list[dict] = []
        skipped = 0
        levels_order = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
        try:
            min_idx = levels_order.index(min_level)
        except ValueError:
            min_idx = 0
        for raw in lines:
            rec = _parse_log_json(raw)
            if rec is None:
                skipped += 1
                continue
            lv = str(rec.get("level") or "").upper()
            try:
                lv_idx = levels_order.index(lv)
            except ValueError:
                lv_idx = 0
            if lv_idx < min_idx:
                continue
            if op_filter and op_filter not in raw:
                continue
            events.append(rec)
        return {"ok": True, "data": {
            "events": events, "skipped": skipped,
            "path": str(log_path), "count": len(events),
        }}


def _all_query():
    from skillos.store.index_repo import SkillQuery

    return SkillQuery(limit=10000)


def _all_slugs(engine) -> list[str]:
    return [r.slug for r in engine.db.list_skills(_all_query())]


# 矩阵缓存（status 任务高频，全扫秒级起步）：TTL 与周期上报间隔
# REPORT_INTERVAL_S 对齐——周期上报每 60s 强刷一次缓存，status 任务
# 在两次上报之间永远命中缓存秒回，不再自己串行跑 git 全扫。
_MATRIX_TTL_S = float(REPORT_INTERVAL_S)
_matrix_cache: dict = {"at": -1e9, "payload": []}
# 全扫互斥：周期上报正在扫时，恰逢缓存过期的 status 不再并发开第二路
# git 全扫（两路竞争会把 25s 拖成 31s+），排队等结果直接复用。
_matrix_lock = threading.Lock()


def _matrix_payload(engine: LocalEngine, fresh: bool = False) -> list[dict]:
    """矩阵载荷（status 任务 / drift_changed 事件共用）：含两类徽标（FR-1.0.19/20）。

    fresh=True 强制全扫（写操作后/周期上报）；否则命中 TTL 内缓存。
    锁内双重检查：等锁期间别的线程已刷完缓存则直接复用，不重复扫。
    """
    if not fresh and (time.monotonic() - _matrix_cache["at"]) < _MATRIX_TTL_S:
        return _matrix_cache["payload"]
    with _matrix_lock:
        if not fresh and (time.monotonic() - _matrix_cache["at"]) < _MATRIX_TTL_S:
            return _matrix_cache["payload"]
        cells = engine.status_matrix()
        slug_by_id = {r.id: r.slug for r in engine.db.list_skills(_all_query())}
        payload = [
            {
                "slug": slug_by_id.get(c.skill_id, c.skill_id),
                "runtime_key": c.runtime_key,
                "state": c.state.name if hasattr(c.state, "name") else str(c.state),
                "name": slug_by_id.get(c.skill_id),
                "installed_version": getattr(c, "installed_version", None),
                "has_untracked_assets": bool(getattr(c, "has_untracked_assets", False)),
                "adapter_outdated": bool(getattr(c, "adapter_outdated", False)),
            }
            for c in cells
        ]
        _matrix_cache["at"] = time.monotonic()
        _matrix_cache["payload"] = payload
        return payload


def drift_payload(engine: LocalEngine, fresh: bool = True) -> list[dict]:
    """构造 drift_changed 上报载荷（云端 upsert 的输入形状）。

    事件上报语义是"我刚扫过/刚写完，这是最新状态"，默认强制刷新
    （fresh=True）——顺带把缓存换成最新的，status 任务随后秒回。
    """
    return _matrix_payload(engine, fresh=fresh)


def check_environment(home: Path) -> list[str]:
    """启动环境校验（WSL 边界 / 云同步目录 / 编码），致命问题返回清单并拒绝启动。"""
    problems: list[str] = []
    markers = ("OneDrive", "iCloud", "Dropbox", "Google Drive")
    repo = (home / ".skillos" / "repo").resolve()
    if any(m in str(part) for part in repo.parts for m in markers):
        problems.append(f"仓库位于云同步目录: {repo}（05 P0-5）")
    if "WSL_DISTRO_NAME" in os.environ and str(repo).startswith("\\\\wsl"):
        problems.append("WSL 内跨 Windows UNC 路径（05 P0-6）")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return problems


def token_path(home: Path) -> Path:
    return home / ".skillos" / "token"


def acquire_instance_lock(home: Path) -> FileLock:
    """本机单实例守卫：同一 home（同一台机器/沙盒）只允许一个 daemon 进程。

    为什么云端 4001 踢不够：踢 → 落败方按普通断连重连 → 反踢回去，
    两个实例互踢成重连风暴（还会把云端 Session 池打满）。必须在源头
    不让第二个实例起来。filelock 与写锁同款（services/locking，禁
    fcntl / msvcrt）；进程退出（含崩溃）由 OS 自动释放，无孤儿锁。
    锁文件写 pid（filelock 默认行为），报错时读出来方便定位旧进程。
    """
    lock_dir = home / ".skillos"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_dir / "daemon.lock"), timeout=1)
    try:
        lock.acquire()
    except Timeout:
        holder = ""
        try:
            holder = (lock_dir / "daemon.lock").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        log.error(
            "instance_lock_busy",
            holder_pid=holder or None,
        )
        sys.exit(1)
    return lock


def load_token(home: Path) -> str | None:
    p = token_path(home)
    if not p.exists():
        return None
    tok = p.read_text(encoding="utf-8").strip()
    return tok or None


def save_token(home: Path, token: str) -> None:
    """token 落盘 0600（Windows 上尽力而为；禁止写入项目目录，NFR-2）。"""
    p = token_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token, encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass  # Windows 无 POSIX 权限位；文件在用户 profile 下即满足隔离


async def run(cloud_url: str, pair_code: str | None = None,
              home: Path | None = None) -> None:
    home = home or resolve_home()
    problems = check_environment(home)
    if problems:
        for p in problems:
            log.error(
                "env_check_failed",
                problem=p,
            )
        sys.exit(1)

    # 单实例锁必须持满进程生命周期（变量只作持有引用，故下划线开头）
    _instance_lock = acquire_instance_lock(home)

    engine = LocalEngine(default_env(home), home=home)
    executor = TaskExecutor(engine)
    token = load_token(home)
    agent_id = f"{platform.system().lower()}-{socket.gethostname().lower()}"
    backoff = 1.0
    user_id: str | None = None

    # 一次 daemon 启动一个 op_id（生命周期）
    bind_op_id(component="daemon", agent_id=agent_id, cloud_url=cloud_url)
    log.info(
        "daemon_started",
        cloud_url=cloud_url, agent_id=agent_id,
        has_token=bool(token), has_pair_code=bool(pair_code),
    )

    while True:
        try:
            async with websockets.connect(cloud_url, max_size=32 * 1024 * 1024) as ws:
                bind_op_id(component="daemon.ws")  # 每次连接一个新 op_id
                log.info(
                    "ws_connected",
                    cloud_url=cloud_url,
                )
                hello: dict = {
                    "type": "hello", "agent_id": agent_id, "version": "0.1.0",
                    "runtimes": [a.key for a in all_adapters()],
                    "platform": platform.system().lower(),
                }
                if token:
                    hello["token"] = token
                elif pair_code:
                    hello["pair_code"] = pair_code
                else:
                    log.error(
                        "auth_missing",
                    )
                    sys.exit(1)
                await ws.send(json.dumps(hello))

                # 握手响应：paired（新 token）/ welcome（老 token）/ error
                ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if ack.get("type") == "paired":
                    bind_op_id(component="auth_pair")  # 配对一个新 op_id
                    log.info(
                        "pair_started",
                    )
                    token = ack["token"]
                    save_token(home, token)
                    user_id = ack.get("user_id")
                    log.info(
                        "pair_succeeded",
                        user_id=user_id,
                    )
                elif ack.get("type") == "welcome":
                    user_id = ack.get("user_id")
                else:
                    log.error(
                        "auth_failed",
                        reason=str(ack.get("error")),
                    )
                    sys.exit(1)
                backoff = 1.0

                # 连接即上报一次漂移（FR-1.2.6）。
                # 全扫是重活（git 子进程 × N + 全量 hash，秒级~分钟级），
                # 必须放线程池：同步阻塞事件循环会卡死爱websockets 的
                # ping/pong（默认 20s 间隔），40s 后客户端自判死链断开
                # → 重连 → 再全扫 → 重连风暴（徽标永远闪断的根因）。
                payload = await asyncio.to_thread(drift_payload, engine)
                await ws.send(json.dumps({
                    "type": "event", "event": "drift_changed",
                    "user_id": user_id,
                    "payload": {"matrix": payload},
                }))

                async def heartbeat() -> None:  # noqa: B023 —— ws 由外层 with 管理生命周期
                    while True:
                        await asyncio.sleep(HEARTBEAT_S)
                        await ws.send(json.dumps({"type": "ping"}))

                async def periodic_report(uid: str | None = user_id) -> None:
                    while True:
                        await asyncio.sleep(REPORT_INTERVAL_S)
                        # 同上：全扫放线程池，不阻塞事件循环（ping/pong 生命线）
                        p = await asyncio.to_thread(drift_payload, engine)
                        await ws.send(json.dumps({
                            "type": "event", "event": "drift_changed",
                            "user_id": uid,
                            "payload": {"matrix": p},
                        }))

                hb = asyncio.create_task(heartbeat())
                rep = asyncio.create_task(periodic_report())
                try:
                    while True:
                        raw = await ws.recv()
                        msg = json.loads(raw)
                        mtype = msg.get("type")
                        if mtype == "pong":
                            continue
                        if mtype == "task":
                            request_id = msg.get("request_id")
                            if not request_id:
                                # 守卫③：无 request_id 直接拒绝（07 N12）
                                log.warning(
                                    "task_rejected_no_request_id",
                                    task_id=msg.get("task_id"),
                                )
                                await ws.send(json.dumps({
                                    "type": "result",
                                    "task_id": msg.get("task_id"),
                                    "ok": False,
                                    "error": {"code": "INVALID_INPUT",
                                              "message": "任务缺少 request_id"},
                                }))
                                continue
                            if executor.dedup.seen(request_id):
                                log.info(
                                    "task_dedup_skipped",
                                    request_id=request_id,
                                    op=msg.get("op"),
                                )
                                continue
                            op_name = str(msg.get("op") or "")
                            # 任务级 op_id（与上层 tags 合并而非覆盖）
                            bind_op_id(component="task", op=op_name, request_id=request_id)
                            log.info(
                                "task_started",
                                op=op_name, request_id=request_id,
                                task_id=msg.get("task_id"),
                            )
                            executor.dedup.mark(request_id, msg.get("task_id"))
                            # 任务执行（scan/status/push 都是重活）放线程池：
                            # status 全扫秒级起步，同步跑会卡死 ping/pong 断链
                            t0 = time.monotonic()
                            result = await asyncio.to_thread(
                                executor.execute,
                                op_name, msg.get("params") or {},
                            )
                            duration_ms = int((time.monotonic() - t0) * 1000)
                            ok = bool(result.get("ok"))
                            error_code = (
                                (result.get("error") or {}).get("code")
                                if not ok else None
                            )
                            log.info(
                                "task_completed",
                                op=op_name, request_id=request_id,
                                ok=ok, error_code=error_code,
                                duration_ms=duration_ms,
                            )
                            payload = {
                                "type": "result",
                                "task_id": msg.get("task_id"),
                                "request_id": request_id,
                                **result,
                            }
                            await ws.send(json.dumps(payload))
                            # 写任务完成后即时上报漂移（FR-1.2.6：push 后必报，
                            # 不等 60s 周期 —— Web 端要立刻看到矩阵收敛）。
                            # 注意此处不能用 TaskExecutor.WRITE_OPS（那是"executor 层
                            # 需补锁"的集合）；这里要的是"产生写入"的集合。
                            if op_name in {"push", "rollback", "pull", "tag_create"}:
                                p2 = await asyncio.to_thread(drift_payload, engine)
                                await ws.send(json.dumps({
                                    "type": "event", "event": "drift_changed",
                                    "user_id": user_id,
                                    "payload": {"matrix": p2},
                                }))
                finally:
                    hb.cancel()
                    rep.cancel()
                    reset_op_context()
                    log.info(
                        "ws_disconnected",
                    )
        except websockets.ConnectionClosed as e:
            # 4001 = 同 agent_id 新连接接管（FR-1.2.7）。退出而非重连：
            # 两个实例会互踢（踢→1s 重连→踢回），形成重连风暴 + 池连接泄漏。
            if e.rcvd is not None and e.rcvd.code == 4001:
                log.error(
                    "ws_superseded_4001",
                    reason="同 agent_id 新连接接管——是否启动了第二个 daemon 实例？",
                )
                sys.exit(1)
            log.warning(
                "ws_reconnect",
                reason=type(e).__name__,
                backoff_s=backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, WS_BACKOFF_MAX_S)
        except (
            ConnectionRefusedError,
            OSError,
            TimeoutError,
        ) as e:
            log.warning(
                "ws_reconnect",
                reason=type(e).__name__,
                backoff_s=backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, WS_BACKOFF_MAX_S)
        except SystemExit:
            raise


def main(cloud_url: str | None = None, pair_code: str | None = None) -> None:
    """daemon 入口（FR-1.2.x）。"""
    from skillos.services.engine import resolve_home

    home = resolve_home()
    setup_logging("daemon", home=home)
    url = cloud_url or "ws://127.0.0.1:9080/ws/agent"
    try:
        asyncio.run(run(url, pair_code))
    except KeyboardInterrupt:
        log.info(
            "daemon_stopped",
            reason="KeyboardInterrupt",
        )


if __name__ == "__main__":
    import typer

    typer.run(main)
