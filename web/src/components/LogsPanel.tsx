import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { endpoints, type LogEvent } from "../api/endpoints";
import { useDaemonStatusCtx } from "../ws/DaemonStatusContext";

/** Web 日志查看面板（FR-1.1.12 / 04 §6.2.5）：
 *
 * 与 Dashboard "同一页面编排"原则一致（08 §4 R2）：日志面板挂在 Dashboard
 * 同一页面的下半部（治理视图之后），侧栏入口。**不新增页面**。
 *
 * 设计要点：
 * - 5s 轮询（前端拉取；不订阅 daemon WS，与漂移事件走不同通道——日志量远大）
 * - level / op_id 客户端过滤（云端也会过滤；客户端二次过滤避免大列表）
 * - 限制展示 200 条，避免页面卡
 * - daemon 离线（503 DAEMON_OFFLINE）→ 提示而不崩
 */

interface Props {
  defaultTail?: number;
}

export default function LogsPanel({ defaultTail = 200 }: Props) {
  const daemon = useDaemonStatusCtx();
  const [level, setLevel] = useState<"DEBUG" | "INFO" | "WARNING" | "ERROR">("INFO");
  const [opFilter, setOpFilter] = useState<string>("");
  const [mode, setMode] = useState<"daemon" | "api" | "mcp">("daemon");

  const logsQuery = useQuery({
    queryKey: ["logs", mode, defaultTail, level],
    queryFn: () =>
      endpoints.logsView({ mode, tail: 1000, level }),
    // 5s 轮询：与漂移事件频率分离；daemon 离线不重试（503 属预期）
    refetchInterval: 5_000,
    retry: false,
  });

  // 客户端按 op_id 过滤（云端只接受精确 op_id；空匹配 = 不过滤）
  const events: LogEvent[] = (logsQuery.data?.events ?? [])
    .filter((ev) =>
      opFilter.trim() ? String(ev.op_id ?? "").includes(opFilter.trim()) : true,
    )
    .slice(-defaultTail);

  const isMissing = logsQuery.data?.missing === true;
  const offline = !daemon.connected;

  return (
    <div
      className="mt-md rounded-[var(--radius-card)] border p-md"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <div className="flex items-center justify-between gap-md">
        <h2
          className="font-semibold"
          style={{ fontSize: "var(--font-h2)", color: "var(--text-primary)" }}
        >
          daemon 日志
        </h2>
        <div className="flex items-center gap-sm">
          <ModeSelect mode={mode} onChange={setMode} disabled={offline} />
          <LevelSelect level={level} onChange={setLevel} disabled={offline} />
          <input
            type="text"
            placeholder="op_id 过滤"
            value={opFilter}
            onChange={(e) => setOpFilter(e.target.value)}
            disabled={offline}
            className="rounded-[var(--radius-control)] border px-sm py-xs text-[var(--font-caption)]"
            style={{
              borderColor: "var(--border)",
              background: "var(--surface-alt)",
              color: "var(--text-primary)",
            }}
          />
        </div>
      </div>
      <p
        className="mt-xs text-[var(--font-caption)]"
        style={{ color: "var(--text-secondary)" }}
      >
        {offline
          ? "daemon 离线 · 日志暂不可查（503 DAEMON_OFFLINE）"
          : isMissing
            ? `${mode}.log 尚未创建（首次运行后才有）`
            : `5s 轮询 · 显示最近 ${events.length} 条`}
      </p>
      <ul
        className="mt-sm flex flex-col gap-xs overflow-auto"
        style={{ maxHeight: 360 }}
      >
        {events.length === 0 && !offline && !isMissing && (
          <li style={{ color: "var(--text-secondary)" }}>暂无匹配日志</li>
        )}
        {events
          .slice()
          .reverse()
          .map((ev, i) => (
            <li
              key={`${ev.ts}-${i}`}
              className="flex items-baseline gap-sm text-[var(--font-mono)]"
              style={{ fontSize: "var(--font-caption)" }}
            >
              <span style={{ color: "var(--text-secondary)", minWidth: 80 }}>
                {ev.ts?.slice(11, 19) ?? "??:??:??"}
              </span>
              <LevelChip level={ev.level} />
              <span style={{ minWidth: 110, color: "var(--text-secondary)" }}>
                {ev.logger}
              </span>
              <span style={{ color: "var(--text-primary)", flex: 1 }}>
                {ev.event}
              </span>
              {ev.op_id && (
                <span
                  style={{
                    color: "var(--primary)",
                    fontSize: "var(--font-caption)",
                  }}
                >
                  {String(ev.op_id).slice(0, 16)}
                </span>
              )}
            </li>
          ))}
      </ul>
    </div>
  );
}

function LevelChip({ level }: { level: string }) {
  const color =
    level === "ERROR" || level === "CRITICAL"
      ? "var(--danger)"
      : level === "WARNING"
        ? "var(--warning)"
        : level === "INFO"
          ? "var(--success)"
          : "var(--text-secondary)";
  return (
    <span
      className="rounded-[var(--radius-tag)] px-xs"
      style={{
        color,
        border: `1px solid ${color}`,
        minWidth: 56,
        textAlign: "center",
      }}
    >
      {level}
    </span>
  );
}

function ModeSelect({
  mode,
  onChange,
  disabled,
}: {
  mode: "daemon" | "api" | "mcp";
  onChange: (m: "daemon" | "api" | "mcp") => void;
  disabled: boolean;
}) {
  return (
    <select
      value={mode}
      onChange={(e) => onChange(e.target.value as "daemon" | "api" | "mcp")}
      disabled={disabled}
      className="rounded-[var(--radius-control)] border px-sm py-xs text-[var(--font-caption)]"
      style={{
        borderColor: "var(--border)",
        background: "var(--surface-alt)",
        color: "var(--text-primary)",
      }}
    >
      <option value="daemon">daemon</option>
      <option value="api">api</option>
      <option value="mcp">mcp</option>
    </select>
  );
}

function LevelSelect({
  level,
  onChange,
  disabled,
}: {
  level: "DEBUG" | "INFO" | "WARNING" | "ERROR";
  onChange: (l: "DEBUG" | "INFO" | "WARNING" | "ERROR") => void;
  disabled: boolean;
}) {
  return (
    <select
      value={level}
      onChange={(e) =>
        onChange(e.target.value as "DEBUG" | "INFO" | "WARNING" | "ERROR")
      }
      disabled={disabled}
      className="rounded-[var(--radius-control)] border px-sm py-xs text-[var(--font-caption)]"
      style={{
        borderColor: "var(--border)",
        background: "var(--surface-alt)",
        color: "var(--text-primary)",
      }}
    >
      <option value="DEBUG">DEBUG+</option>
      <option value="INFO">INFO+</option>
      <option value="WARNING">WARNING+</option>
      <option value="ERROR">ERROR+</option>
    </select>
  );
}

// 标记 useEffect import 不被 unused warning 误删（仅留 hook 占位）
void useEffect;