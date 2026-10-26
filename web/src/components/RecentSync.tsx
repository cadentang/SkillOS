/**
 * 最近同步记录（设计稿 app/index.html §03 .recent__row）。
 *
 *  数据源：daemon 本地结构化日志（endpoints.logsView, mode=daemon）。
 *  筛选事件 = sync_completed / sync_partial / sync_failed（P2 / FR-1.1.12）。
 *  daemon 离线 → 降级为「暂无记录」（读操作保留，FR-2.4.4）。
 *  离线判定走 DaemonStatusContext（WS 心跳，与徽标/LogsPanel 同一口径）；
 *  HTTP 请求失败但 WS 在线 ≠ daemon 离线（502 TIMEOUT / 401 / 网络抖动），
 *  不得谎报离线。
 *
 *  视觉：
 *    .recent  flex-col
 *      .recent__row  h-42px · flex items-center · gap-md
 *        .recent__time w-52px font-mono caption 颜色 tertiary
 *        .recent__desc flex-1 truncate
 *        .recent__status font-caption 500 + 颜色 t-success / t-warning / t-danger / t-tertiary
 */

import { useQuery } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { endpoints, type LogEvent } from "../api/endpoints";
import { useDaemonStatusCtx } from "../ws/DaemonStatusContext";

/** 读日志失败的可读文案。
 *
 * `TIMEOUT` 与离线是两件事（见文件头）：daemon 明明连着、只是没在时限内回包，
 * 混成一句「记录读取失败」会让人误判成网络/断连。 */
function logsErrText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === "TIMEOUT") return "daemon 未回包（超时）· 稍后重试";
    return `日志读取失败（${err.code}）· 稍后重试`;
  }
  return "记录读取失败 · 稍后重试";
}

type RowKind = "ok" | "warning" | "danger" | "tertiary";

interface RecentRow {
  time: string;
  desc: string;
  kind: RowKind;
  ts: number; // for sort
}

/** 解析一行日志：仅挑 sync 相关事件，提取 op + 文本。 */
function parseSyncEvent(ev: LogEvent): RecentRow | null {
  const e = String(ev.event ?? "");
  if (!e.startsWith("sync_")) return null;

  let desc = "";
  let kind: RowKind = "ok";

  switch (e) {
    case "sync_completed": {
      const op = String(ev.op ?? "push");
      const target = String(ev.target ?? "");
      const files = ev.file_count ?? ev.files ?? "";
      desc = `${op}  ${target ? target + " · " : ""}${files ? files + " files" : ""}`.trim();
      kind = "ok";
      break;
    }
    case "sync_partial": {
      const op = String(ev.op ?? "push");
      const target = String(ev.target ?? "");
      desc = `${op}  ${target} · 部分 Runtime 已同步`;
      kind = "warning";
      break;
    }
    case "sync_failed": {
      const op = String(ev.op ?? "push");
      const target = String(ev.target ?? "");
      const reason = String(ev.reason ?? ev.error_code ?? "失败");
      desc = `${op}  ${target} · ${reason}`;
      kind = "danger";
      break;
    }
    default:
      return null;
  }

  const at = ev.ts ?? "";
  // HH:MM from ISO
  let time = "";
  const m = /T(\d{2}):(\d{2})/.exec(at);
  if (m) time = `${m[1]}:${m[2]}`;
  return { time, desc, kind, ts: Date.parse(at) || 0 };
}

const STATUS_COLOR: Record<RowKind, string> = {
  ok: "var(--success)",
  warning: "var(--warning)",
  danger: "var(--danger)",
  tertiary: "var(--text-tertiary)",
};

const STATUS_LABEL: Record<RowKind, string> = {
  ok: "成功",
  warning: "部分",
  danger: "冲突中止",
  tertiary: "—",
};

export default function RecentSync() {
  const daemon = useDaemonStatusCtx();
  const logs = useQuery({
    queryKey: ["recent-sync"],
    queryFn: () => endpoints.logsView({ mode: "daemon", tail: 60, level: "INFO" }),
    refetchInterval: 30_000,
    retry: false,
  });

  const rows: RecentRow[] = (logs.data?.events ?? [])
    .map(parseSyncEvent)
    .filter((r): r is RecentRow => !!r)
    .sort((a, b) => b.ts - a.ts)
    .slice(0, 6);

  return (
    <div
      className="flex flex-col"
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-card)",
        padding: 20,
      }}
    >
      <div className="flex items-center justify-between">
        <span className="font-semibold" style={{ fontSize: "var(--font-h3)" }}>
          最近同步记录
        </span>
        <span
          className="text-[var(--font-caption)] font-medium"
          style={{ color: "var(--primary)" }}
        >
          查看全部
        </span>
      </div>

      <div className="mt-sm flex flex-col">
        {rows.length === 0 ? (
          <div
            className="h-[42px] flex items-center text-[var(--font-caption)]"
            style={{ color: "var(--text-tertiary)" }}
          >
            {!daemon.connected
              ? "daemon 离线 · 暂无记录"
              : logs.isError
                ? logsErrText(logs.error)
                : "暂无最近同步记录"}
          </div>
        ) : (
          rows.map((r, i) => (
            <div
              key={i}
              className="flex items-center gap-md"
              style={{ height: 42 }}
            >
              <span
                className="w-[52px] shrink-0 font-mono text-[var(--font-caption)]"
                style={{ color: "var(--text-tertiary)" }}
              >
                {r.time || "--:--"}
              </span>
              <span
                className="min-w-0 flex-1 truncate text-[13px]"
                title={r.desc}
              >
                {r.desc}
              </span>
              <span
                className="shrink-0 text-[var(--font-caption)] font-medium"
                style={{ color: STATUS_COLOR[r.kind] }}
              >
                {STATUS_LABEL[r.kind]}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
