import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints, type ConflictDecision, type PushResultItem } from "../api/endpoints";
import { ApiError } from "../api/client";
import ConflictResolveDialog from "../components/ConflictResolveDialog";
import CreateSkillDialog from "../components/CreateSkillDialog";
import DriftMatrix from "../components/DriftMatrix";
import RemovalsConfirmDialog from "../components/RemovalsConfirmDialog";
import { StateDot, stateLabel } from "../components/StateChip";
import { useDaemonStatusCtx } from "../ws/DaemonStatusContext";

/** Dashboard（FR-2.1.x）—— 双读者同一页面两种编排（08 §4 R2，不新增页面）：
 *  上半部 OverviewStats + DriftMatrix + BatchActionBar（写 Skill 的人）
 *  下半部 ManagerPanel（决策者：覆盖度 / 僵尸占比 / 门禁计数）
 *  daemon 离线（FR-2.4.4）：读保留 + stale 标注；写按钮全局禁用。 */

type ItemState = "pending" | "running" | "ok" | "needs-confirm" | "failed";

interface PushItem {
  slug: string;
  state: ItemState;
  message?: string;
  removals: string[];
}

export default function Dashboard() {
  const daemon = useDaemonStatusCtx();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [items, setItems] = useState<PushItem[] | null>(null);
  const [confirmPaths, setConfirmPaths] = useState<string[] | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  /** CONFLICT 四选一（P3）：点 CONFLICT 格 → 弹 dialog 选定 → 调 API */
  const [resolveTarget, setResolveTarget] = useState<
    { slug: string; runtimeKey: string } | null
  >(null);

  const drift = useQuery({
    queryKey: ["drift"],
    queryFn: endpoints.driftStatus,
  });
  const gate = useQuery({
    queryKey: ["gate"],
    queryFn: endpoints.gateSummary,
    retry: false, // daemon 离线时 503 属预期，不重试
  });
  const health = useQuery({
    queryKey: ["health"],
    queryFn: endpoints.health,
    retry: false,
    staleTime: Infinity, // 沙盒标志部署期不变（与 Layout 共享缓存）
  });

  const rows = drift.data?.rows ?? [];
  const summary = drift.data?.summary ?? {};
  const manager = drift.data?.manager;

  const toggle = (slug: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(slug)) next.delete(slug);
      else next.add(slug);
      return next;
    });
  const toggleAll = (checked: boolean) =>
    setSelected(checked ? new Set(rows.map((r) => r.slug)) : new Set());

  const pushMutation = useMutation({
    mutationFn: ({
      skillIds,
      confirmRemovals,
    }: {
      skillIds: string[];
      confirmRemovals?: string[];
    }) => endpoints.push(skillIds, confirmRemovals),
  });

  /** 沙盒重置（P7 / FR-3.10）：仅沙盒模式渲染按钮；成功后全量失效刷新 */
  const sandboxResetMutation = useMutation({
    mutationFn: () => endpoints.sandboxReset(),
    onSuccess: () => {
      void queryClient.invalidateQueries();
    },
  });

  const resolveMutation = useMutation({
    mutationFn: ({
      slug,
      runtimeKey,
      decision,
    }: {
      slug: string;
      runtimeKey: string;
      decision: ConflictDecision;
    }) => endpoints.resolveConflict(slug, runtimeKey, decision),
    onSuccess: () => {
      setResolveTarget(null);
      void drift.refetch();
    },
  });

  /** 逐项分发（FR-2.1.4）：单 Skill 冲突不阻塞其余 —— 好的照常分发，
   *  冲突/待确认项进「需要处理」列表。 */
  async function runPush(slugs: string[], confirmFor?: Record<string, string[]>) {
    const initial: PushItem[] = slugs.map((s) => ({
      slug: s,
      state: "pending",
      removals: [],
    }));
    setItems(initial);
    for (const item of initial) {
      setItems((prev) =>
        prev!.map((p) => (p.slug === item.slug ? { ...p, state: "running" } : p)),
      );
      try {
        const data = await pushMutation.mutateAsync({
          skillIds: [item.slug],
          confirmRemovals: confirmFor?.[item.slug],
        });
        const failed = data.results.filter((r: PushResultItem) => !r.ok);
        const removals = data.results.flatMap(
          (r: PushResultItem) => r.held_back_removals ?? [],
        );
        setItems((prev) =>
          prev!.map((p) =>
            p.slug === item.slug
              ? {
                  ...p,
                  state: failed.length === 0 && removals.length === 0 ? "ok" : "needs-confirm",
                  removals,
                  message:
                    failed[0]?.error?.message ??
                    (removals.length ? "有待确认的删除项" : undefined),
                }
              : p,
          ),
        );
      } catch (err) {
        const apiErr = err instanceof ApiError ? err : null;
        const removals = (apiErr?.detail as { held_back_removals?: string[] })
          ?.held_back_removals ?? [];
        setItems((prev) =>
          prev!.map((p) =>
            p.slug === item.slug
              ? {
                  ...p,
                  state: removals.length ? "needs-confirm" : "failed",
                  removals,
                  message: apiErr?.message ?? String(err),
                }
              : p,
          ),
        );
      }
    }
    void drift.refetch();
    // 汇总全部待确认删除路径 → 弹 RemovalsConfirmDialog（FR-2.4.7）
    setItems((prev) => {
      const paths = (prev ?? []).flatMap((p) => p.removals);
      if (paths.length) setConfirmPaths(paths);
      return prev;
    });
  }

  /** 弹窗确认后：只对「needs-confirm」的 skill 重发，带精确 confirm_removals。 */
  async function confirmRemovals() {
    const targets = (items ?? []).filter((p) => p.state === "needs-confirm");
    const paths = confirmPaths ?? [];
    setConfirmPaths(null);
    const confirmFor: Record<string, string[]> = {};
    for (const t of targets) confirmFor[t.slug] = t.removals;
    await runPush(
      targets.map((t) => t.slug),
      confirmFor,
    );
    void paths;
  }

  const gateTotals = useMemo(() => {
    const s = gate.data?.summary;
    if (!s) return null;
    let errors = 0;
    let warns = 0;
    for (const v of Object.values(s)) {
      errors += v.errors;
      warns += v.warns;
    }
    return { errors, warns, skills: Object.keys(s).length };
  }, [gate.data]);

  const stale = drift.data?.stale ?? false;
  const sandbox = health.data?.sandbox ?? false;

  return (
    <div className="flex flex-col gap-lg">
      {/* ---------- 上半部：写 Skill 的人读 ---------- */}
      <section>
        <div className="flex items-baseline justify-between">
          <h1 className="font-semibold" style={{ fontSize: "var(--font-h1)" }}>
            能力总览
          </h1>
          {stale && (
            <span
              className="rounded-[var(--radius-tag)] px-sm py-xs text-[var(--font-caption)]"
              style={{ background: "var(--surface-alt)", color: "var(--warning)" }}
            >
              daemon 离线 · 数据可能过期（FR-2.4.4）
            </span>
          )}
        </div>

        {/* OverviewStats：总数 / 状态分布 */}
        <div className="mt-md flex flex-wrap gap-md">
          <StatCard label="Skill 总数" value={manager?.total_skills ?? rows.length} />
          {Object.entries(summary)
            .sort((a, b) => b[1] - a[1])
            .map(([state, n]) => (
              <StatCard
                key={state}
                label={<StateDot state={state} />}
                value={n}
              />
            ))}
        </div>

        {/* BatchActionBar（FR-2.1.4）：离线全局禁写 */}
        <div
          className="mt-md flex items-center gap-md rounded-[var(--radius-control)] border px-md py-sm"
          style={{ borderColor: "var(--border)", background: "var(--surface)" }}
        >
          <button
            disabled={!daemon.connected || selected.size === 0 || pushMutation.isPending}
            onClick={() => runPush([...selected])}
            className="rounded-[var(--radius-control)] px-md py-sm font-medium text-[var(--inverse)] disabled:opacity-40"
            style={{ background: "var(--primary)" }}
          >
            一键分发（{selected.size}）
          </button>
          <button
            disabled={!daemon.connected}
            onClick={() => setShowCreate(true)}
            className="rounded-[var(--radius-control)] px-md py-sm disabled:opacity-40"
            style={{ background: "var(--surface)", border: "1px solid var(--primary-border)", color: "var(--primary)" }}
            title="新建 Skill（FR-2.1.7）：创建后用「一键分发」推到四端"
          >
            + 新建 Skill
          </button>
          {sandbox && (
            <button
              disabled={!daemon.connected || sandboxResetMutation.isPending}
              onClick={() => sandboxResetMutation.mutate()}
              className="rounded-[var(--radius-control)] px-md py-sm disabled:opacity-40"
              style={{ background: "var(--surface)", border: "1px solid var(--border)", color: "var(--warning)" }}
              title="重置沙盒数据（FR-3.10）：恢复预置演示数据，评委可反复体验"
            >
              {sandboxResetMutation.isPending ? "重置中…" : "重置沙盒数据"}
            </button>
          )}
          {sandboxResetMutation.isError && (
            <span className="text-[var(--font-caption)]" style={{ color: "var(--danger)" }}>
              重置失败：{sandboxResetMutation.error instanceof Error
                ? sandboxResetMutation.error.message : "未知错误"}
            </span>
          )}
          <span className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
            {!daemon.connected
              ? "本地 daemon 已离线，写操作禁用"
              : selected.size === 0
                ? "勾选矩阵行后分发；单 Skill 冲突不阻塞其余"
                : "分发 canonical → 四端 Runtime"}
          </span>
        </div>

        {/* 逐项进度与结果（FR-2.1.4） */}
        {items && (
          <div
            className="mt-md rounded-[var(--radius-control)] border px-md py-sm"
            style={{ borderColor: "var(--border)", background: "var(--surface)" }}
          >
            <p className="font-medium">分发结果</p>
            <ul className="mt-sm flex flex-col gap-xs">
              {items.map((p) => (
                <li key={p.slug} className="flex items-center gap-sm">
                  <span style={{ minWidth: 140 }} className="font-mono text-[var(--font-mono)]">
                    {p.slug}
                  </span>
                  <span
                    className="text-[var(--font-caption)]"
                    style={{
                      color:
                        p.state === "ok"
                          ? "var(--success)"
                          : p.state === "failed"
                            ? "var(--danger)"
                            : p.state === "needs-confirm"
                              ? "var(--warning)"
                              : "var(--text-secondary)",
                    }}
                  >
                    {p.state === "pending" && "等待中…"}
                    {p.state === "running" && "分发中…"}
                    {p.state === "ok" && "已分发"}
                    {p.state === "needs-confirm" && `需要处理（${p.removals.length} 个删除待确认）`}
                    {p.state === "failed" && `失败：${p.message ?? ""}`}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* DriftMatrix（FR-2.1.2） */}
        <div
          className="mt-md rounded-[var(--radius-card)] border p-md"
          style={{ background: "var(--surface)", borderColor: "var(--border)" }}
        >
          <DriftMatrix rows={rows} selected={selected} onToggle={toggle} onToggleAll={toggleAll}
          onResolveConflict={(slug, runtimeKey) => setResolveTarget({ slug, runtimeKey })} />
        </div>
      </section>

      {/* ---------- 下半部：决策者读（ManagerPanel，同一页面 FR-2.1.1） ---------- */}
      <section>
        <h2 className="font-semibold" style={{ fontSize: "var(--font-h2)" }}>
          治理视图
        </h2>
        <div className="mt-md flex flex-wrap gap-md">
          <StatCard label="覆盖度（同步格 / 全部格）" value={`${manager?.coverage_pct ?? 0}%`} />
          <StatCard label="僵尸 Skill 占比" value={`${manager?.zombie_pct ?? 0}%`} />
          <StatCard
            label="门禁 error"
            value={gateTotals ? gateTotals.errors : "—"}
            accent={gateTotals && gateTotals.errors > 0 ? "var(--danger)" : undefined}
          />
          <StatCard
            label="门禁 warn"
            value={gateTotals ? gateTotals.warns : "—"}
            accent={gateTotals && gateTotals.warns > 0 ? "var(--warning)" : undefined}
          />
        </div>
        {manager && (
          <p className="mt-sm text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
            健康度分布：
            {Object.entries(manager.health)
              .map(([s, n]) => `${stateLabel(s)} ${n}`)
              .join(" · ") || "暂无"}
            {gateTotals ? ` · 门禁覆盖 ${gateTotals.skills} 个 Skill` : " · daemon 离线，门禁计数不可用"}
          </p>
        )}
      </section>

      {/* CreateSkillDialog（P3，FR-2.1.7）：新建入口，对话框不是新页面 */}
      {showCreate && (
        <CreateSkillDialog
          onCancel={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            // 新 Skill 入矩阵（未分发态）；门禁汇总与治理视图一并失效
            void queryClient.invalidateQueries({ queryKey: ["drift"] });
            void queryClient.invalidateQueries({ queryKey: ["gate"] });
          }}
        />
      )}

      {/* RemovalsConfirmDialog（FR-2.4.7）：限高 + 滚动 + 按钮常在屏内 */}
      {confirmPaths && confirmPaths.length > 0 && (
        <RemovalsConfirmDialog
          paths={confirmPaths}
          onCancel={() => setConfirmPaths(null)}
          onConfirm={confirmRemovals}
        />
      )}

      {/* ConflictResolveDialog（P3，FR-2.2.6 / T22）：CONFLICT 四选一 */}
      {resolveTarget && (
        <ConflictResolveDialog
          slug={resolveTarget.slug}
          runtimeKey={resolveTarget.runtimeKey}
          onCancel={() => setResolveTarget(null)}
          submitting={resolveMutation.isPending}
          onConfirm={(decision) =>
            resolveMutation.mutate({
              slug: resolveTarget.slug,
              runtimeKey: resolveTarget.runtimeKey,
              decision,
            })
          }
        />
      )}
    </div>
  );
}

function StatCard({
  label,
  value,
  accent,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  accent?: string;
}) {
  return (
    <div
      className="min-w-[140px] rounded-[var(--radius-card)] border px-md py-sm"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <p className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
        {label}
      </p>
      <p
        className="mt-xs font-semibold"
        style={{ fontSize: "var(--font-h2)", color: accent ?? "var(--text-primary)" }}
      >
        {value}
      </p>
    </div>
  );
}
