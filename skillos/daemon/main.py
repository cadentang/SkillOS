"""daemon 主循环（FR-1.2.x，04 §2.5.2）。

出站 WS 连云端：
- 握手：优先长期 token；无 token 时走一次性配对码（--pair）
- 任务执行器：**领域操作白名单**（WS_TASK_OPS），无 request_id 直接拒绝，
  写操作持全局写锁（FR-1.2.4）、request_id 近窗口 LRU 去重（07 N12 守卫②③）
- 心跳保活 + 断线指数退避重连（1s,2s,4s,…,60s 上限，FR-1.2.5）
- 连接建立后上报一次漂移矩阵；push 后与周期性再报（FR-1.2.6）
- 用量上报（N-01.4/9）：连上即补报近 30 天聚合行、每日聚合后再报一次；
  开关 `usage_report_enabled` 关闭时**一条都不发**（零上报可断言）

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
from datetime import UTC, datetime
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
from skillos.services.usage_service import VIA_WEB

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
    # N-01.1：只有「skill 级」调用计入遥测。仪表盘类（scan/status/gate_summary/logs）
    # 与诊断类（check/decay）不计 —— 否则「零调用」语义会被自己的面板污染。
    USAGE_OPS = frozenset({
        "version_log", "diff", "file_tree", "file_history", "file_read", "file_write",
        "push", "pull", "rollback", "resolve_conflict", "skill_create", "skill_delete",
        "tag_create",
    })

    def __init__(self, engine: LocalEngine) -> None:
        self.engine = engine
        self.dedup = RequestDedup()

    def execute(self, op: str, params: dict) -> dict:
        """返回 result 载荷（{"ok":..., "data"/"error"}）。异常不外抛。"""
        if op not in WS_TASK_OPS:
            return {"ok": False, "error": {"code": "INVALID_INPUT",
                                           "message": f"非白名单操作: {op}"}}
        result = self._run(op, params)
        self._record_usage(op, params, result)
        return result

    def _run(self, op: str, params: dict) -> dict:
        try:
            if op in self.WRITE_OPS:
                with write_lock(self.engine.paths.lock_dir, timeout=LOCK_TIMEOUT_S):
                    return self._dispatch(op, params)
            return self._dispatch(op, params)
        except Exception as e:  # noqa: BLE001 —— result 信封承载错误，不让 daemon 崩
            log.exception("task %s failed", op)
            return {"ok": False, "error": {"code": "IO", "message": str(e)}}

    def _record_usage(self, op: str, params: dict, result: dict) -> None:
        """L1 采集（N-01 / V2-T2）：Web 侧发起的 skill 级调用在这里一次收口。

        遥测**不得影响任务结果**：失败只记日志（N-11.4 的反向要求）。
        """
        if op not in self.USAGE_OPS:
            return
        slug = str(params.get("slug") or "")
        if not slug:
            return
        try:
            self.engine.usage.record(
                slug, op, via=VIA_WEB, ok=bool(result.get("ok")), runtime="web",
            )
        except Exception:  # noqa: BLE001 —— 采集失败绝不影响任务返回
            log.warning("usage_record_failed", op=op, slug=slug)

    def _dispatch(self, op: str, params: dict) -> dict:
        engine = self.engine
        if op == "usage_report":
            # N-01.4/5：只回聚合行；开关关闭时**拒绝**（云端据此断言零上报）
            if not engine.settings.report_enabled():
                return {"ok": False, "error": {
                    "code": "REPORT_DISABLED",
                    "message": "设备侧未开启 usage_report_enabled（默认 off，N-01.5）",
                }}
            return {"ok": True, "data": {"rows": engine.usage.report_rows()}}
        if op == "usage_aggregate":
            # 5.2：只接受 params.truncated（不接任意参数/SQL）；重算在前、清理在后
            rows = engine.usage.aggregate()
            purged = engine.usage.purge()
            return {"ok": True, "data": {"rows": rows, "purged": purged}}
        if op == "settings_get":
            # V2-T1：全部登记键一次性返回（键集来自 settings_service.DEFAULTS，不猜）
            from skillos.services.settings_service import DEFAULTS

            return {"ok": True, "data": {
                "items": {key: engine.settings.get(key) for key in DEFAULTS},
            }}
        if op == "settings_put":
            # 批量写显式值（T13 精神）：先全量校验键名再写，避免半批生效
            items = params.get("items")
            if not isinstance(items, dict) or not items:
                return {"ok": False, "error": {
                    "code": "INVALID_INPUT",
                    "message": "settings_put 需要 items（{key: value}，显式值）",
                }}
            from skillos.services.settings_service import DEFAULTS

            unknown = sorted(k for k in items if k not in DEFAULTS)
            if unknown:
                return {"ok": False, "error": {
                    "code": "INVALID_INPUT",
                    "message": f"未登记的开关键: {unknown}（先更新 docs/12 §3）",
                }}
            for key, value in items.items():
                engine.settings.set(key, value)
            return {"ok": True, "data": {
                "items": {key: engine.settings.get(key) for key in items},
            }}
        if op == "usage_summary":
            days, bad = _int_param(params, "days", 30, lo=1, hi=180)
            if bad:
                return bad
            slugs = engine.all_slugs()
            return {"ok": True, "data": {
                "days": days,
                "total_skills": len(slugs),
                # N-01.8：两个榜单都按 30 天窗口（days 只影响这两个）
                "top": engine.usage.top_called(slugs=slugs, days=days, limit=10),
                "zero_called": engine.usage.zero_called(slugs=slugs, days=days),
                # N-01.7：口径单点 = usage_service.zombie_slugs（双条件）
                "zombie": engine.usage.zombie_slugs(
                    slugs=slugs, degraded=engine.degraded_slugs(),
                ),
                "zombie_basis": "实测（90 天零调用）+ 折旧（staleness=degraded）",
            }}
        if op == "usage_events":
            slug = str(params.get("slug") or "")
            if not slug:
                return {"ok": False, "error": {
                    "code": "INVALID_INPUT", "message": "usage_events 需要 slug",
                }}
            limit, bad = _int_param(params, "limit", 100, lo=1, hi=500)
            if bad:
                return bad
            return {"ok": True, "data": {
                "slug": slug,
                "events": engine.usage.events_for(slug, limit=limit),
                "sessions": engine.usage.session_chains(slug, limit=limit),
            }}
        if op == "report_list":
            limit, bad = _int_param(params, "limit", 20, lo=1, hi=100)
            if bad:
                return bad
            return {"ok": True, "data": {"items": engine.reports.list_reports(limit=limit)}}
        if op == "report_get":
            report_id = str(params.get("id") or "")
            report = engine.reports.get_report(report_id) if report_id else None
            if report is None:
                return {"ok": False, "error": {
                    "code": "NOT_FOUND", "message": f"报告不存在: {report_id}",
                }}
            return {"ok": True, "data": report}
        if op == "rules_list":
            # N-03/N-08 规则面板：生效规则 + 无效规则（含原因）+ 白名单 + ruleset_hash
            return {"ok": True, "data": engine.rules.panel_dict()}
        if op == "similarity_list":
            # E-04.3：只给分数与建议读数，不做任何变更（N-07.2 用户永远可改）
            # N-07.5：已弃用清单带 skill_id —— 前端"恢复"直接调既有 push 路由，
            # 不新增一个只为转发的端点（恢复 = 重新分发）
            deprecated = [
                {"slug": r.slug, "id": r.id}
                for r in engine.db.list_skills(_all_query()) if r.deprecated
            ]
            return {"ok": True, "data": {
                "items": engine.similarity.list_pairs(),
                "deprecated": deprecated,
            }}
        if op == "skill_deprecate":
            # N-07.3 回收落选方：dry_run 先列 removals（runtime_key:path），确认后执行
            if not engine:
                return {"ok": False, "error": {
                    "code": "NOT_INITIALIZED", "message": "引擎未初始化",
                }}
            slug = str(params.get("slug") or "")
            if not slug:
                return {"ok": False, "error": {"code": "INVALID_INPUT", "message": "缺少 slug"}}
            raw = params.get("confirm_removals") or []
            if not isinstance(raw, list):
                return {"ok": False, "error": {
                    "code": "INVALID_INPUT", "message": "confirm_removals 必须是字符串数组",
                }}
            try:
                data = engine.deprecate_skill(
                    slug,
                    dry_run=bool(params.get("dry_run", True)),
                    confirmed_removals=[str(p) for p in raw],
                )
            except SkillValidationError as e:
                return {"ok": False, "error": {"code": e.code, "message": e.message}}
            if not data.get("ok"):
                return {"ok": False, "error": {
                    "code": str(data.get("error_code") or "DEPRECATE_FAILED"),
                    "message": str(data.get("detail") or ""),
                    "removals": data.get("removals") or [],
                }}
            return {"ok": True, "data": data}
        if op == "similarity_run":
            # TQ-03：详情页「立即比对」手动触发；覆盖式落库 ⇒ 重复点不新增行
            run = engine.similarity.run()
            return {"ok": True, "data": {
                "pairs": run.pairs, "added": run.added, "skills": run.skills,
                "threshold": run.threshold,
            }}
        if op == "upstream_check":
            # E-08.1：手动「立即检查」（单 skill）或全量。网络 IO 不在任何锁内（T17）
            from skillos.services.upstream_service import UpstreamService

            service = UpstreamService(engine.db, engine.git)
            slug = str(params.get("slug") or "")
            if slug:
                return {"ok": True, "data": service.check(slug).to_dict()}
            return {"ok": True, "data": service.check_all()}
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
                    # E-07.1/2：category 进 frontmatter，template ∈ 内置三模板
                    category=str(params.get("category") or ""),
                    template=str(params.get("template") or "minimal"),
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
        """逐条门禁 findings（FR-1.0.11 / NFR-10.5：与 push 共用同一判定入口）。

        M3 起包含自定义规则（N-03）与安全规则（N-08），结果落库并带 `ruleset_hash`；
        检查尾部触发一次相似重算（E-04.2 / TQ-03：独立于 check 性能基准，
        失败只记日志 —— 相似提示坏了不该影响检查结论）。
        """
        slug = str(params.get("slug") or "")
        security_only = bool(params.get("security_only", False))
        # `include_untracked` 默认 True（显式 check 要扫大文件，N-08.3）；
        # CR 合并前置传 False —— 与 push 前置同一口径（E-03.4 / E-05.2），
        # 不为了一次合并多读一遍超阈值资产。
        include_untracked = bool(params.get("include_untracked", True))
        try:
            report = self.engine.checks.check(
                slug, security_only=security_only, include_untracked=include_untracked,
            )
        except Exception as e:  # noqa: BLE001 —— canonical 缺失等场景转 INVALID_INPUT
            return {"ok": False, "error": {"code": "INVALID_INPUT", "message": str(e)}}

        try:
            self.engine.run_similarity()
        except Exception:  # noqa: BLE001 —— 相似重算是旁路能力
            log.warning("similarity_refresh_failed", skill_slug=slug)

        return {"ok": True, "data": {
            "has_error": report.gate.has_error,
            "ruleset_hash": report.bundle.ruleset_hash,
            "findings": [
                {"rule": f.rule, "level": str(f.level), "message": f.message}
                for f in report.gate.findings
            ],
            "invalid_rules": report.bundle.invalid,
            "ignored": report.bundle.ignored,
            "decay": report.decay.to_dict(),
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


def _int_param(params: dict, key: str, default: int, *,
               lo: int, hi: int) -> tuple[int, dict | None]:
    """取整数参数并夹紧到 `[lo, hi]`；不是整数就报 INVALID_INPUT（不猜、不兜底）。"""
    raw = params.get(key, default)
    if raw is None:
        raw = default
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default, {"ok": False, "error": {
            "code": "INVALID_INPUT", "message": f"{key} 必须是整数",
        }}
    return max(lo, min(raw, hi)), None


def _seconds_since(at: datetime) -> float:
    """距今秒数；列在 SQLite 里读回是 naive（存的时候丢了 tz），这里两种都认。"""
    now = datetime.now(UTC) if at.tzinfo else datetime.now(UTC).replace(tzinfo=None)
    return (now - at).total_seconds()


async def push_usage_report(ws, engine: LocalEngine) -> int:
    """主动上行一次用量聚合行（N-01.4 每日 / N-01.9 重连补报）。

    返回发送行数：**0 表示没发**（`usage_report_enabled=false` 时
    `report_rows()` 就是空列表）—— 于是"零上报"在协议层可被证明，
    而不是靠"没人调用"。报文只有聚合值，无事件、无参数摘要、无路径。
    """
    rows = await asyncio.to_thread(engine.usage.report_rows)
    if not rows:
        return 0
    await ws.send(json.dumps({
        "type": "event", "event": "usage_report",
        "payload": {"rows": rows},
    }))
    return len(rows)


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

                # N-01.9 断连补报：连上即报近 30 天全量，云端覆盖合并 ⇒ 重发不翻倍。
                # 开关关闭时 push_usage_report 直接返回 0、不发送（零上报）。
                try:
                    sent = await push_usage_report(ws, engine)
                    if sent:
                        log.info("usage_report_sent", rows=sent, trigger="reconnect")
                except Exception:  # noqa: BLE001 —— 补报失败不影响连接主循环
                    log.warning("usage_report_send_failed", trigger="reconnect")

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

                async def usage_tick() -> None:  # noqa: B023 —— ws 由外层 with 管理生命周期
                    """5.2：每日 00:10（本地时）重算聚合并清理 30 天前事件；
                    聚合成功后**顺手上报**一次（N-01.4 每日上报）。

                    每分钟醒一次、当天只跑一次；落库与上报都走异步/线程，不占事件循环。
                    上报失败只记日志 —— 明天全量重报覆盖（N-01.9），不丢不重。
                    """
                    last_run_day = None
                    while True:
                        now = datetime.now().astimezone()
                        due = (now.hour, now.minute) >= (0, 10)
                        if due and last_run_day != now.date():
                            try:
                                await asyncio.to_thread(
                                    executor.execute, "usage_aggregate", {}
                                )
                                last_run_day = now.date()
                            except Exception:  # noqa: BLE001 —— 维护任务失败不拖垮连接
                                log.exception("usage_aggregate_failed")
                            try:
                                sent = await push_usage_report(ws, engine)
                                if sent:
                                    log.info("usage_report_sent", rows=sent,
                                             trigger="daily")
                            except Exception:  # noqa: BLE001 —— 上报失败次日补
                                log.warning("usage_report_send_failed", trigger="daily")
                        await asyncio.sleep(60)

                async def report_tick() -> None:
                    """N-02.1：按 `report.cadence`（daily/weekly，默认 weekly）生成体检报告。

                    计算只读、不持写锁（N-02.5 / T17）；生成失败只记日志，
                    下一轮到期判定会重试（基线取上一份报告，不会因失败丢数据）。
                    首启无历史报告时先生成一份基线，否则"周报"要等到装完一周才有。
                    """
                    cadence_s = {"daily": 86_400, "weekly": 7 * 86_400}
                    while True:
                        await asyncio.sleep(600)
                        try:
                            cadence = str(engine.settings.get("report.cadence") or "weekly")
                            span = cadence_s.get(cadence)
                            if span is None:
                                log.warning("report_cadence_unknown", cadence=cadence)
                                span = cadence_s["weekly"]
                            last = await asyncio.to_thread(
                                engine.db.list_health_reports, 1
                            )
                            due = not last or _seconds_since(last[0].period_end) >= span
                            if due:
                                report = await asyncio.to_thread(engine.reports.generate)
                                log.info(
                                    "report_generated",
                                    report_id=report.get("id"),
                                    unchanged=report.get("unchanged"),
                                    cadence=cadence,
                                )
                        except Exception:  # noqa: BLE001 —— 报告失败不拖垮连接
                            log.exception("report_generate_failed")

                async def upstream_tick() -> None:
                    """E-08.1：上游检查每日一次（本地时 03:00 后首个 tick）。

                    受 settings `upstream.check_enabled` 控制（默认开）；关掉即完全不跑，
                    纯 CLI 单机没有 daemon ⇒ 只能手动检查（E-08.1 的两种形态）。
                    检查本身是网络 IO，放线程池，不占事件循环（T17）。
                    """
                    last_run_day = None
                    while True:
                        await asyncio.sleep(300)
                        try:
                            enabled = bool(engine.settings.get("upstream.check_enabled"))
                        except Exception:  # noqa: BLE001 —— 设置读失败当作关闭
                            enabled = False
                        now = datetime.now().astimezone()
                        if not enabled or now.hour < 3 or last_run_day == now.date():
                            continue
                        try:
                            result = await asyncio.to_thread(
                                executor.execute, "upstream_check", {},
                            )
                            last_run_day = now.date()
                            data = result.get("data") or {}
                            log.info(
                                "upstream_daily_check",
                                checked=data.get("checked"),
                                summary=data.get("summary"),
                            )
                        except Exception:  # noqa: BLE001 —— 下次 tick 重试
                            log.exception("upstream_daily_check_failed")

                hb = asyncio.create_task(heartbeat())
                rep = asyncio.create_task(periodic_report())
                agg = asyncio.create_task(usage_tick())
                rpt = asyncio.create_task(report_tick())
                upt = asyncio.create_task(upstream_tick())
                try:
                    while True:
                        raw = await ws.recv()
                        msg = json.loads(raw)
                        mtype = msg.get("type")
                        if mtype == "pong":
                            continue
                        if mtype == "event_ack":
                            # 上线事件的回执（N-01.4）：merged/skipped 落日志，
                            # 让"报上去了"有证据，而不是只知道"发出去了"
                            _err = (msg.get("error") or {}).get("code")
                            if msg.get("ok"):
                                log.info(
                                    "usage_report_acked",
                                    event=msg.get("event"),
                                    merged=msg.get("merged"),
                                    skipped=msg.get("skipped"),
                                )
                            else:
                                log.warning(
                                    "usage_report_rejected",
                                    event=msg.get("event"),
                                    error_code=_err,
                                )
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
                    agg.cancel()
                    rpt.cancel()
                    upt.cancel()
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
