import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { endpoints, type HealthReportSummary } from "../api/endpoints";
import { ApiError } from "../api/client";

/** 体检报告（N-02 / docs/12 §5.5）。
 *
 *  左列表 + 右详情。报告由 daemon 按 `report.cadence` 生成（或 CLI `skillos report`），
 *  云端只代理读取（红线 5：不碰磁盘、不缓存文件）。
 *
 *  两种「空」必须分清：
 *  - 列表为空 = 还没生成过（daemon 首启会先落一份基线）；
 *  - 报告 `unchanged` = 这一期确实没有变化（N-02.6 仍要留痕，不是失败）。
 */

function fmt(at: string | null): string {
  if (!at) return "—";
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? at : d.toLocaleString();
}

export default function Reports() {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const list = useQuery({
    queryKey: ["reports"],
    queryFn: () => endpoints.reports(20),
    retry: false,
  });
  const items = list.data?.items ?? [];
  const activeId = selectedId ?? items[0]?.id ?? null;

  const detail = useQuery({
    queryKey: ["report", activeId],
    queryFn: () => endpoints.report(activeId!),
    enabled: !!activeId,
    retry: false,
  });

  const listError = list.error;
  const offline =
    listError instanceof ApiError && listError.code === "DAEMON_OFFLINE";

  return (
    <div className="flex h-full flex-col gap-md">
      <div className="flex items-baseline justify-between">
        <h1 className="font-semibold" style={{ fontSize: "var(--font-h1)" }}>
          体检报告
        </h1>
        <button
          type="button"
          onClick={() => void list.refetch()}
          className="rounded-[var(--radius-control)] px-sm py-xs text-[var(--font-caption)]"
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            color: "var(--text-secondary)",
          }}
        >
          刷新
        </button>
      </div>

      {offline && (
        <p
          className="rounded-[var(--radius-control)] px-sm py-sm text-[var(--font-caption)]"
          style={{ color: "var(--warning)", background: "var(--surface-alt)" }}
        >
          daemon 离线：报告保存在本机，连上后才能查看与生成。
        </p>
      )}
      {list.isError && !offline && (
        <p className="text-[13px]" style={{ color: "var(--danger)" }}>
          读取失败：{listError instanceof Error ? listError.message : String(listError)}
        </p>
      )}

      <div className="grid min-h-0 flex-1 gap-md lg:grid-cols-[320px_1fr]">
        {/* 列表 */}
        <div
          className="flex min-h-0 flex-col overflow-auto rounded-[var(--radius-card)] border"
          style={{ borderColor: "var(--border)", background: "var(--surface)" }}
        >
          {items.length === 0 ? (
            <p className="p-md text-[13px]" style={{ color: "var(--text-secondary)" }}>
              还没有报告。daemon 首启会落一份基线；也可以在本机执行
              <code className="mx-xs">skillos report</code>
              立刻生成。
            </p>
          ) : (
            items.map((item) => (
              <ReportRow
                key={item.id}
                item={item}
                active={item.id === activeId}
                onClick={() => setSelectedId(item.id)}
              />
            ))
          )}
        </div>

        {/* 详情 */}
        <div
          className="flex min-h-0 flex-col gap-md overflow-auto rounded-[var(--radius-card)] border p-md"
          style={{ borderColor: "var(--border)", background: "var(--surface)" }}
        >
          {detail.isLoading && <p className="text-[13px]">加载中…</p>}
          {detail.isError && (
            <p className="text-[13px]" style={{ color: "var(--danger)" }}>
              详情读取失败：
              {detail.error instanceof Error ? detail.error.message : String(detail.error)}
            </p>
          )}
          {detail.data && <ReportDetail data={detail.data} />}
          {!activeId && !detail.isLoading && (
            <p className="text-[13px]" style={{ color: "var(--text-secondary)" }}>
              左侧选择一份报告查看增量明细。
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function ReportRow({
  item,
  active,
  onClick,
}: {
  item: HealthReportSummary;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex flex-col gap-xs border-b px-sm py-sm text-left"
      style={{
        borderColor: "var(--divider)",
        background: active ? "var(--surface-alt)" : "transparent",
      }}
    >
      <span className="flex items-center justify-between gap-sm">
        <span className="text-[13px] font-medium">{fmt(item.period_end)}</span>
        {item.unchanged ? (
          <Badge text="无变化" color="var(--text-secondary)" />
        ) : (
          <Badge
            text={`${item.new_drift + item.new_gate_errors + item.newly_degraded} 项变化`}
            color="var(--warning)"
          />
        )}
      </span>
      <span className="flex flex-wrap gap-sm text-[var(--font-caption)]"
            style={{ color: "var(--text-secondary)" }}>
        <span>漂移 +{item.new_drift}</span>
        <span>门禁 +{item.new_gate_errors}</span>
        <span>折旧 +{item.newly_degraded}</span>
        <span>健康度 {item.avg_health ?? "—"}</span>
      </span>
    </button>
  );
}

function Badge({ text, color }: { text: string; color: string }) {
  return (
    <span
      className="rounded-[var(--radius-control)] px-xs text-[var(--font-caption)]"
      style={{ color, border: `1px solid ${color}` }}
    >
      {text}
    </span>
  );
}

function ReportDetail({ data }: { data: Awaited<ReturnType<typeof endpoints.report>> }) {
  const drift = data.drift;
  const gate = data.gate;
  const decay = data.decay;
  const health = data.health;

  return (
    <div className="flex flex-col gap-md">
      <div className="flex flex-wrap items-baseline justify-between gap-sm">
        <span className="font-medium">
          {fmt(data.period?.start)} → {fmt(data.period?.end)}
        </span>
        {data.unchanged && <Badge text="本期无变化（N-02.6 仍留痕）" color="var(--text-secondary)" />}
      </div>

      <Section title="① 漂移增量（MODIFIED / CONFLICT / MISSING）">
        {drift.unavailable && (
          <Muted text={`本期漂移不可统计：${drift.unavailable}`} />
        )}
        {!drift.unavailable && Object.entries(drift.counts).length === 0 ? (
          <Muted text="本期没有可统计的漂移态" />
        ) : (
          Object.entries(drift.counts).map(([state, c]) => (
            <p key={state} className="text-[13px] tabular-nums">
              {state}：{c.now} 项（较上期 {c.delta >= 0 ? "+" : ""}
              {c.delta}）
            </p>
          ))
        )}
        {drift.new.map((item) => (
          <p key={`${item.slug}-${item.runtime_key}`} className="text-[12px]"
             style={{ color: "var(--warning)" }}>
            + {item.state} · {item.slug} @ {item.runtime_key}
          </p>
        ))}
        {drift.new_count > drift.new.length && (
          <Muted text={`另有 ${drift.new_count - drift.new.length} 项未显示（Top 10 截断）`} />
        )}
      </Section>

      <Section title="② 门禁 error 新增">
        {gate.new.length === 0 ? (
          <Muted text="本期没有新增 error" />
        ) : (
          gate.new.map((item) => (
            <p key={`${item.slug}-${item.rule}`} className="text-[12px]"
               style={{ color: "var(--danger)" }}>
              + {item.slug} · {item.rule}
            </p>
          ))
        )}
        <Muted text={`当前 error 共 ${gate.errors_now} 项`} />
      </Section>

      <Section title="③ 折旧 ok → degraded">
        {decay.newly_degraded.length === 0 ? (
          <Muted text="本期没有新增折旧" />
        ) : (
          decay.newly_degraded.map((slug) => (
            <p key={slug} className="text-[12px]" style={{ color: "var(--warning)" }}>
              + {slug}
            </p>
          ))
        )}
        <Muted text={`当前 degraded 共 ${decay.degraded_now} 项`} />
      </Section>

      <Section title={`④ 健康度降幅 Top5（均值 ${health.avg ?? "—"}）`}>
        {health.drops.length === 0 ? (
          <Muted text="本期健康度没有下降" />
        ) : (
          health.drops.map((d) => (
            <p key={d.slug} className="text-[12px] tabular-nums">
              {d.slug}：{d.from} → {d.to}（{d.delta >= 0 ? "+" : ""}
              {d.delta}）
            </p>
          ))
        )}
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-xs border-t pt-sm" style={{ borderColor: "var(--divider)" }}>
      <span className="font-medium text-[13px]">{title}</span>
      {children}
    </div>
  );
}

function Muted({ text }: { text: string }) {
  return (
    <p className="text-[var(--font-caption)]" style={{ color: "var(--text-tertiary)" }}>
      {text}
    </p>
  );
}
