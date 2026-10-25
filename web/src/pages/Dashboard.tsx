import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  endpoints,
  type ConflictDecision,
  type PushResultItem,
  type SimilarityItem,
} from "../api/endpoints";
import { ApiError } from "../api/client";
import ConflictResolveDialog from "../components/ConflictResolveDialog";
import CreateSkillDialog from "../components/CreateSkillDialog";
import DriftMatrix from "../components/DriftMatrix";
import LogsPanel from "../components/LogsPanel";
import RecentSync from "../components/RecentSync";
import RemovalsConfirmDialog from "../components/RemovalsConfirmDialog";
import { stateLabel } from "../components/StateChip";
import { useDaemonStatusCtx } from "../ws/DaemonStatusContext";

/** Dashboard（FR-2.1.x / 设计稿 app/index.html §03）。
 *
 *  业务 topbar：title + 搜索 + 重新扫描 + 一键分发
 *  上半（写 Skill 的人）：3 个 Stat card + 漂移矩阵 + 最近同步记录
 *  下半（决策者，FR-2.1.1 双读者同一页面）：治理视图
 *  日志面板（FR-1.1.12）
 *  daemon 离线 → 写按钮全局禁用（FR-2.4.4）
 *
 *  业务逻辑全保留（批量分发 / conflict 四选一 / removals 守卫 / create skill 等），
 *  仅重写视觉与少量布局。
 */

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
  /** CONFLICT 四选一（P3 / FR-2.2.6）：点 CONFLICT 格 → 弹 dialog 选定 → 调 API */
  const [resolveTarget, setResolveTarget] = useState<
    { slug: string; runtimeKey: string } | null
  >(null);
  /** 全局搜索（FR-2.1.6）：本地模糊过滤 name / description / tag，本期纯前端预览 */
  const [search, setSearch] = useState("");

  const drift = useQuery({
    queryKey: ["drift"],
    queryFn: endpoints.driftStatus,
    // 兜底轮询：与 daemon 周期上报 REPORT_INTERVAL_S=60s 对齐；
    // 实时性靠 WS drift_changed 事件失效，不靠拉高轮询频率
    refetchInterval: 60_000,
  });
  const gate = useQuery({
    queryKey: ["gate"],
    queryFn: endpoints.gateSummary,
    retry: false, // daemon 离线时 503 属预期，不重试
    refetchInterval: 60_000, // 同 drift：事件驱动为主，60s 兜底
  });
  const health = useQuery({
    queryKey: ["health"],
    queryFn: endpoints.health,
    retry: false,
    staleTime: Infinity, // 沙盒标志部署期不变（与 Layout 共享缓存）
  });
  /** N-01.7/8 管理者视图：Top / 零调用 / 僵尸（本地数据，daemon 离线时不可用）。 */
  const usage = useQuery({
    queryKey: ["usage", "manager"],
    queryFn: () => endpoints.managerUsage(30),
    retry: false,
    refetchInterval: 60_000,
  });
  /** E-04.3「疑似重复」：只读数 + 提示，回收动作走人工路径（N-07.2 永不自动执行）。 */
  const similarity = useQuery({
    queryKey: ["similarity"],
    queryFn: endpoints.similarity,
    retry: false,
    refetchInterval: 60_000,
  });
  const usageData = usage.data;
  /** 僵尸占比只认新口径（实测 + 折旧）；离线显示 —，不回落旧口径混着看 */
  const zombiePct =
    usageData && usageData.total_skills > 0
      ? Math.round((usageData.zombie.length / usageData.total_skills) * 100)
      : null;

  const similarItems = similarity.data?.items ?? [];
  const deprecatedList = similarity.data?.deprecated ?? [];
  const similarityOffline =
    similarity.error instanceof ApiError && similarity.error.code === "DAEMON_OFFLINE";

  const allRows = drift.data?.rows ?? [];
  const filteredRows = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return allRows;
    return allRows.filter((r) => r.slug.toLowerCase().includes(q));
  }, [allRows, search]);
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
    setSelected(checked ? new Set(filteredRows.map((r) => r.slug)) : new Set());

  const pushMutation = useMutation({
    mutationFn: ({
      skillIds,
      confirmRemovals,
    }: {
      skillIds: string[];
      confirmRemovals?: string[];
    }) => endpoints.push(skillIds, confirmRemovals),
  });

  /** 沙盒重置（P7 / E-06.4）：202 + task_id → 轮询到终态；失败要看到原因（含自证差异） */
  const [sandboxStatus, setSandboxStatus] = useState<string | null>(null);
  const sandboxResetMutation = useMutation({
    mutationFn: async () => {
      const started = await endpoints.sandboxReset();
      setSandboxStatus("重置中…");
      const taskId = started.task_id;
      for (let i = 0; i < 150; i += 1) {
        await new Promise((r) => setTimeout(r, 2000));
        const st = await endpoints.sandboxResetStatus(taskId);
        if (st.status === "succeeded") {
          setSandboxStatus(
            `重置完成${st.result?.seed_version ? `（seed ${st.result.seed_version}）` : ""}`,
          );
          return st;
        }
        if (st.status === "failed") {
          setSandboxStatus(null);
          throw new Error(st.error ?? "重置失败");
        }
      }
      setSandboxStatus(null);
      throw new Error("重置超时（5 分钟）——请查看 daemon 日志");
    },
    onSuccess: () => {
      void queryClient.invalidateQueries();
    },
  });

  /** 详情页/总览的「立即比对」（TQ-03）：覆盖式落库 ⇒ 重复点不新增行。 */
  const similarityRunMutation = useMutation({
    mutationFn: () => endpoints.similarityRun(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["similarity"] });
    },
  });

  /** 回收落选方（N-07.3）：两段式 —— 先 dry_run 列 removals，用户确认后才真跑。 */
  const [deprecateTarget, setDeprecateTarget] = useState<
    { a: string; b: string; keep: string; removals: string[] } | null
  >(null);
  /** 仅查看差异（N-07 GWT③）：双栏只读对比，不产生任何变更 */
  const [diffTarget, setDiffTarget] = useState<{ a: string; b: string } | null>(null);
  const [similarityError, setSimilarityError] = useState<string | null>(null);

  const deprecateMutation = useMutation({
    mutationFn: (v: {
      a: string; b: string; keep: string; dryRun: boolean; confirmRemovals?: string[];
    }) => endpoints.similarityResolve(v.a, v.b, v.keep, {
      dryRun: v.dryRun, confirmRemovals: v.confirmRemovals,
    }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["similarity"] });
      void queryClient.invalidateQueries({ queryKey: ["drift"] });
    },
  });

  /** N-07.5 恢复：重新分发即恢复，不新增端点（push 成功 daemon 会清弃用标记）。 */
  const restoreMutation = useMutation({
    mutationFn: (skillId: string) => endpoints.push([skillId]),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["similarity"] });
      void queryClient.invalidateQueries({ queryKey: ["drift"] });
    },
  });

  async function previewDeprecate(item: SimilarityItem, keep: string) {
    setSimilarityError(null);
    const drop = keep === item.a ? item.b : item.a;
    try {
      const res = await endpoints.similarityResolve(item.a, item.b, keep, { dryRun: true });
      setDeprecateTarget({ a: item.a, b: item.b, keep, removals: res.removals ?? [] });
      if ((res.removals ?? []).length === 0) {
        setSimilarityError(
          `${drop} 没有可回收的 Runtime 副本（可能尚未分发过），确认后将只打弃用标记。`,
        );
      }
    } catch (e) {
      setSimilarityError(`预览失败：${e instanceof Error ? e.message : String(e)}`);
    }
  }

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
  const totalSkills = manager?.total_skills ?? allRows.length;

  // 优先高严重度状态汇总（用于 Stat card drift alarm）
  const driftAlarm = (summary.CONFLICT ?? 0) + (summary.OUTDATED ?? 0) + (summary.MODIFIED ?? 0) + (summary.MISSING ?? 0);

  return (
    <div className="flex flex-col gap-lg">
      {/* ─────── 业务 topbar：title + 搜索 + 重新扫描 + 一键分发 ─────── */}
      <div className="flex items-center justify-between">
        <div className="flex items-baseline gap-sm">
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
        <div className="flex items-center gap-sm">
          {/* 全局搜索（FR-2.1.6）—— 设计稿 search 形态 */}
          <div
            className="flex h-9 items-center gap-sm"
            style={{
              width: 280,
              padding: "0 12px",
              background: "var(--surface-alt)",
              border: "1px solid var(--border)",
              borderRadius: "var(--radius-control)",
            }}
          >
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden>
              <circle cx="7" cy="7" r="4.5" stroke="var(--text-tertiary)" strokeWidth="1.6" />
              <path d="M10.5 10.5L14 14" stroke="var(--text-tertiary)" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="搜索 Skill…"
              className="min-w-0 flex-1 bg-transparent text-[13px] outline-none"
              style={{ color: "var(--text-primary)" }}
            />
          </div>
          <button
            type="button"
            disabled={!daemon.connected}
            onClick={() => void drift.refetch()}
            className="h-9 rounded-[var(--radius-control)] border px-md text-[13px] font-medium disabled:opacity-40"
            style={{ background: "var(--surface)", borderColor: "var(--border)", color: "var(--text-primary)" }}
          >
            重新扫描
          </button>
          <button
            type="button"
            disabled={!daemon.connected || selected.size === 0 || pushMutation.isPending}
            onClick={() => runPush([...selected])}
            className="h-9 rounded-[var(--radius-control)] px-md text-[13px] font-medium text-[var(--inverse)] disabled:opacity-40"
            style={{ background: "var(--primary)" }}
          >
            一键分发（{selected.size}）
          </button>
        </div>
      </div>

      {/* ─────── 上半：能力总览（写者） ─────── */}
      <section>
        {/* 3 个 stat card（设计稿 .stat 248宽 gap-16） */}
        <div className="flex flex-wrap gap-md">
          <StatCard
            label="Skill 总数"
            value={totalSkills}
            delta={`已纳管 ${allRows.length} · 漂移告警 ${driftAlarm}`}
          />
          <StatCard
            label="漂移告警"
            value={driftAlarm}
            accent={driftAlarm > 0 ? "var(--danger)" : undefined}
            delta={
              `冲突 ${summary.CONFLICT ?? 0} · ` +
              `未同步 ${summary.OUTDATED ?? 0} · ` +
              `手改 ${summary.MODIFIED ?? 0}`
            }
          />
          <StatCard
            label="daemon 状态"
            value={daemon.connected ? "在线" : "离线"}
            accent={daemon.connected ? "var(--success)" : "var(--danger)"}
            delta={daemon.connected ? "WS 已建立 · 实时事件驱动" : "WS 未连接 · 数据可能过期"}
          />
        </div>

        {/* 漂移矩阵 card（设计稿 .card + matrix 表格 44 行高 / col 118） */}
        <div
          className="mt-md flex flex-col rounded-[var(--radius-card)] border"
          style={{ background: "var(--surface)", borderColor: "var(--border)" }}
        >
          <div className="flex items-center justify-between p-md">
            <div>
              <div className="font-semibold" style={{ fontSize: "var(--font-h2)" }}>
                四端漂移矩阵
              </div>
              <div
                className="mt-xs text-[var(--font-caption)]"
                style={{ color: "var(--text-tertiary)" }}
              >
                行 = Skill · 列 = Runtime · 格 = 漂移状态
              </div>
            </div>
            <div className="flex items-center gap-md">
              <LegendItem color="var(--drift-synced)" text="已同步" />
              <LegendItem color="var(--drift-outdated)" text="待分发" />
              <LegendItem color="var(--drift-conflict)" text="需处理" />
            </div>
          </div>
          <div className="h-px w-full" style={{ background: "var(--divider)" }} />
          <div className="px-md py-md">
            <DriftMatrix
              rows={filteredRows}
              selected={selected}
              onToggle={toggle}
              onToggleAll={toggleAll}
              onResolveConflict={(slug, runtimeKey) => setResolveTarget({ slug, runtimeKey })}
            />
          </div>
        </div>

        {/* 最近同步记录 card */}
        <div className="mt-md">
          <RecentSync />
        </div>
      </section>

      {/* ─────── 下半：治理视图（FR-2.1.1 双读者同一页面 / 08 §4 R2） ─────── */}
      <section>
        <h2 className="font-semibold" style={{ fontSize: "var(--font-h2)" }}>
          治理视图
        </h2>
        <div className="mt-md flex flex-wrap gap-md">
          <StatCard
            label="覆盖度（同步格 / 全部格）"
            value={`${manager?.coverage_pct ?? 0}%`}
          />
          <StatCard
            label="僵尸 Skill 占比"
            value={zombiePct !== null ? `${zombiePct}%` : "—"}
            delta={
              usageData
                ? `${usageData.zombie.length} / ${usageData.total_skills} 个`
                : "daemon 离线，本地用量不可用"
            }
          />
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
          <p
            className="mt-sm text-[var(--font-caption)]"
            style={{ color: "var(--text-secondary)" }}
          >
            健康度分布：
            {Object.entries(manager.health)
              .map(([s, n]) => `${stateLabel(s)} ${n}`)
              .join(" · ") || "暂无"}
            {gateTotals ? ` · 门禁覆盖 ${gateTotals.skills} 个 Skill` : " · daemon 离线，门禁计数不可用"}
          </p>
        )}

        {/* N-01.8 两个榜单 + N-01.7 口径标注（管理者视图，数据来自 daemon） */}
        <div className="mt-md grid gap-md lg:grid-cols-2">
          <ListCard
            title="近 30 天调用 Top 10"
            hint={
              usageData
                ? `L1 调用 + L2 会话归属（token 为估算口径）`
                : "daemon 离线，本地用量不可用"
            }
          >
            {(usageData?.top ?? []).map((item, index) => (
              <RankRow
                key={item.skill_id}
                rank={index + 1}
                label={item.skill_id}
                value={`${item.count} 次`}
                hint={`in ${item.tokens_in} / out ${item.tokens_out}`}
              />
            ))}
            {usageData && usageData.top.length === 0 && (
              <EmptyRow text="近 30 天没有调用记录" />
            )}
          </ListCard>

          <ListCard
            title="近 30 天零调用"
            hint={usageData ? `共 ${usageData.zero_called.length} 个 Skill` : "—"}
          >
            {(usageData?.zero_called ?? []).slice(0, 12).map((slug) => (
              <RankRow key={slug} label={slug} value="0 次" />
            ))}
            {usageData && usageData.zero_called.length > 12 && (
              <EmptyRow
                text={`另有 ${usageData.zero_called.length - 12} 个未显示`}
              />
            )}
            {usageData && usageData.zero_called.length === 0 && (
              <EmptyRow text="没有零调用的 Skill" />
            )}
          </ListCard>
        </div>
        <p
          className="mt-sm text-[var(--font-caption)]"
          style={{ color: "var(--text-secondary)" }}
          title="口径单点在 usage_service.zombie_slugs（N-01.7）"
        >
          僵尸口径：{usageData?.zombie_basis ?? "实测（90 天零调用）+ 折旧（staleness=degraded）"}
          {usageData && usageData.zombie.length > 0
            ? ` · 当前 ${usageData.zombie.slice(0, 5).join("、")}${usageData.zombie.length > 5 ? " 等" : ""}`
            : ""}
        </p>
      </section>

      {/* ─────── 疑似重复（E-04.3 / N-07）：只提示与建议，回收永远是人工动作 ─────── */}
      <section>
        <div className="flex items-end justify-between gap-md">
          <div>
            <h2 className="font-semibold" style={{ fontSize: "var(--font-h2)" }}>
              疑似重复
            </h2>
            <p
              className="mt-xs text-[var(--font-caption)]"
              style={{ color: "var(--text-tertiary)" }}
            >
              ≥75% 列出提示 · ≥90% 给合并建议（保留 / 回收由推荐链给出，永不自动执行）
            </p>
          </div>
          <button
            type="button"
            disabled={!daemon.connected || similarityRunMutation.isPending}
            onClick={() => similarityRunMutation.mutate()}
            className="h-9 rounded-[var(--radius-control)] border px-md text-[13px] font-medium disabled:opacity-40"
            style={{
              background: "var(--surface)",
              borderColor: "var(--border)",
              color: "var(--text-primary)",
            }}
            title="立即比对（TQ-03：旁路能力，不计入 check 性能基准）"
          >
            {similarityRunMutation.isPending ? "比对中…" : "立即比对"}
          </button>
        </div>

        <div
          className="mt-md flex flex-col rounded-[var(--radius-card)] border p-md"
          style={{ background: "var(--surface)", borderColor: "var(--border)" }}
        >
          {similarityOffline && (
            <EmptyRow text="daemon 离线：相似对存在本机 skill_similarity，连上后才能读取。" />
          )}
          {similarity.isError && !similarityOffline && (
            <p className="text-[13px]" style={{ color: "var(--danger)" }}>
              读取失败：{similarity.error instanceof Error ? similarity.error.message : "未知错误"}
            </p>
          )}
          {!similarityOffline && !similarity.isError && similarItems.length === 0 && (
            <EmptyRow text="没有 ≥75% 的相似对。新纳管或改动 Skill 后会重新比对。" />
          )}
          {similarityError && (
            <p className="pb-sm text-[var(--font-caption)]" style={{ color: "var(--danger)" }}>
              {similarityError}
            </p>
          )}
          {similarItems.slice(0, 8).map((item) => (
            <SimilarRow
              key={`${item.a}:${item.b}`}
              item={item}
              keep={item.suggestion?.keep ?? null}
              onDeprecate={(keep) => void previewDeprecate(item, keep)}
              onDiff={() => setDiffTarget({ a: item.a, b: item.b })}
            />
          ))}
          {similarItems.length > 8 && (
            <EmptyRow text={`另有 ${similarItems.length - 8} 对未显示（按分数降序）`} />
          )}
          {deprecatedList.length > 0 && (
            <div
              className="flex flex-wrap items-center gap-sm border-t pt-sm"
              style={{ borderColor: "var(--divider)" }}
            >
              <span className="text-[var(--font-caption)]" style={{ color: "var(--text-tertiary)" }}>
                已弃用（canonical 保留，可恢复）
              </span>
              {deprecatedList.map((d) => (
                <span
                  key={d.slug}
                  className="flex items-center gap-xs rounded-[var(--radius-control)] px-xs py-xs text-[var(--font-caption)]"
                  style={{ background: "var(--surface-alt)", color: "var(--text-secondary)" }}
                >
                  {d.slug}
                  <button
                    type="button"
                    disabled={!daemon.connected || restoreMutation.isPending}
                    onClick={() => restoreMutation.mutate(d.id)}
                    className="underline disabled:opacity-40"
                    style={{ color: "var(--primary)" }}
                    title="重新分发即恢复（N-07.5）"
                  >
                    恢复
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>
      </section>

      {/* 日志面板（P5 / FR-1.1.12）：同一页面编排（08 §4 R2） */}
      <LogsPanel defaultTail={200} />

      {/* ─────── 创建 Skill 与分发结果 / 守门弹窗（沿用项目原 dial；保留业务） ─────── */}
      <div
        className="rounded-[var(--radius-control)] border p-md"
        style={{ borderColor: "var(--border)", background: "var(--surface)" }}
      >
        <div className="flex items-center gap-md">
          <button
            type="button"
            disabled={!daemon.connected}
            onClick={() => setShowCreate(true)}
            className="rounded-[var(--radius-control)] px-md py-sm text-[13px] disabled:opacity-40"
            style={{
              background: "var(--surface)",
              border: "1px solid var(--primary-border)",
              color: "var(--primary)",
            }}
            title="新建 Skill（FR-2.1.7）"
          >
            + 新建 Skill
          </button>
          {sandbox && (
            <button
              type="button"
              disabled={!daemon.connected || sandboxResetMutation.isPending}
              onClick={() => sandboxResetMutation.mutate()}
              className="rounded-[var(--radius-control)] px-md py-sm text-[13px] disabled:opacity-40"
              style={{
                background: "var(--surface)",
                border: "1px solid var(--border)",
                color: "var(--warning)",
              }}
              title="重置沙盒数据（FR-3.10）"
            >
              {sandboxResetMutation.isPending ? sandboxStatus ?? "重置中…" : "重置沙盒数据"}
            </button>
          )}
          <span
            className="text-[var(--font-caption)]"
            style={{ color: "var(--text-secondary)" }}
          >
            {!daemon.connected
              ? "本地 daemon 已离线，写操作禁用"
              : selected.size === 0
                ? "勾选矩阵行后用顶部「一键分发」；单 Skill 冲突不阻塞其余"
                : `已选 ${selected.size} 个 Skill，等待分发`}
          </span>
        </div>

        {sandboxResetMutation.isError && (
          <span
            className="mt-sm block text-[var(--font-caption)]"
            style={{ color: "var(--danger)" }}
          >
            重置失败：
            {sandboxResetMutation.error instanceof Error
              ? sandboxResetMutation.error.message
              : "未知错误"}
          </span>
        )}

        {items && (
          <div className="mt-md">
            <p className="font-medium">分发结果</p>
            <ul className="mt-xs flex flex-col gap-xs">
              {items.map((p) => (
                <li key={p.slug} className="flex items-center gap-sm">
                  <span
                    style={{ minWidth: 140 }}
                    className="font-mono text-[var(--font-mono)]"
                  >
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
      </div>

      {showCreate && (
        <CreateSkillDialog
          onCancel={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            void queryClient.invalidateQueries({ queryKey: ["drift"] });
            void queryClient.invalidateQueries({ queryKey: ["gate"] });
          }}
        />
      )}

      {confirmPaths && confirmPaths.length > 0 && (
        <RemovalsConfirmDialog
          paths={confirmPaths}
          onCancel={() => setConfirmPaths(null)}
          onConfirm={confirmRemovals}
        />
      )}

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

      {deprecateTarget && (
        <RemovalsConfirmDialog
          paths={deprecateTarget.removals}
          title="回收落选方（canonical 不动）"
          description={
            `将回收 ${deprecateTarget.keep === deprecateTarget.a
              ? deprecateTarget.b : deprecateTarget.a} 的 Runtime 副本，并打上弃用标记。`
            + " canonical 正文与历史保留，随时可点「恢复」（重新分发）。删除前 daemon 会自动备份。"
          }
          confirmText="确认回收"
          onCancel={() => setDeprecateTarget(null)}
          onConfirm={() => {
            deprecateMutation.mutate({
              a: deprecateTarget.a, b: deprecateTarget.b,
              keep: deprecateTarget.keep, dryRun: false,
              confirmRemovals: deprecateTarget.removals,
            });
            setDeprecateTarget(null);
          }}
        />
      )}

      {diffTarget && (
        <SimilarDiffDialog
          a={diffTarget.a}
          b={diffTarget.b}
          onClose={() => setDiffTarget(null)}
        />
      )}
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────────── */

function StatCard({
  label,
  value,
  delta,
  accent,
}: {
  label: string;
  value: React.ReactNode;
  delta?: React.ReactNode;
  accent?: string;
}) {
  return (
    <div
      className="flex min-w-[200px] flex-1 flex-col gap-xs p-md"
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-card)",
      }}
    >
      <span
        className="text-[var(--font-caption)] font-medium"
        style={{ color: "var(--text-secondary)" }}
      >
        {label}
      </span>
      <span
        className="font-bold tabular-nums leading-tight"
        style={{ fontSize: 26, color: accent ?? "var(--text-primary)" }}
      >
        {value}
      </span>
      {delta && (
        <span
          className="text-[var(--font-caption)]"
          style={{ color: "var(--text-tertiary)" }}
        >
          {delta}
        </span>
      )}
    </div>
  );
}

function ListCard({
  title,
  hint,
  children,
}: {
  title: string;
  hint: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className="flex flex-col gap-sm p-md"
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-card)",
      }}
    >
      <div className="flex items-baseline justify-between gap-sm">
        <span className="font-medium text-[var(--font-body)]">{title}</span>
        <span
          className="text-[var(--font-caption)]"
          style={{ color: "var(--text-secondary)" }}
        >
          {hint}
        </span>
      </div>
      <div className="h-px w-full" style={{ background: "var(--divider)" }} />
      <div className="flex flex-col gap-xs">{children}</div>
    </div>
  );
}

function RankRow({
  rank,
  label,
  value,
  hint,
}: {
  rank?: number;
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="flex items-center justify-between gap-sm py-xs">
      <span className="flex min-w-0 items-center gap-sm">
        {rank !== undefined && (
          <span
            className="shrink-0 text-[var(--font-caption)] tabular-nums"
            style={{ color: "var(--text-secondary)", width: 18 }}
          >
            {rank}
          </span>
        )}
        <span className="truncate text-[13px]">{label}</span>
      </span>
      <span className="flex shrink-0 items-center gap-sm">
        {hint && (
          <span
            className="text-[var(--font-caption)] tabular-nums"
            style={{ color: "var(--text-secondary)" }}
          >
            {hint}
          </span>
        )}
        <span className="text-[13px] tabular-nums">{value}</span>
      </span>
    </div>
  );
}

function EmptyRow({ text }: { text: string }) {
  return (
    <p
      className="py-sm text-[var(--font-caption)]"
      style={{ color: "var(--text-secondary)" }}
    >
      {text}
    </p>
  );
}

/** 一行相似对：分数 + 建议 + 三个动作（N-07）。
 *
 *  动作顺序刻意是「先看差异、再回收」：撤回理由 —— 用户永远可以否决建议（N-07.2）；
 *  回收必须走两段式守卫（先列 removals 再确认，红线 16），本行只负责发起预览。
 */
function SimilarRow({
  item, keep, onDeprecate, onDiff,
}: {
  item: SimilarityItem;
  keep: string | null;
  onDeprecate: (keep: string) => void;
  onDiff: () => void;
}) {
  const drop = keep === item.a ? item.b : item.a;
  return (
    <div
      className="flex flex-col gap-xs border-b py-sm"
      style={{ borderColor: "var(--divider)" }}
    >
      <span className="flex flex-wrap items-baseline gap-sm">
        <span className="text-[13px] font-medium">{item.a}</span>
        <span className="text-[var(--font-caption)]" style={{ color: "var(--text-tertiary)" }}>
          ↔
        </span>
        <span className="text-[13px] font-medium">{item.b}</span>
        <span
          className="text-[var(--font-caption)] tabular-nums"
          style={{ color: item.merge_candidate ? "var(--warning)" : "var(--text-secondary)" }}
        >
          {(item.score * 100).toFixed(1)}%
        </span>
        <span
          className="rounded-[var(--radius-control)] px-xs text-[var(--font-caption)]"
          style={{
            color: item.merge_candidate ? "var(--warning)" : "var(--text-secondary)",
            border: `1px solid ${item.merge_candidate ? "var(--warning)" : "var(--border)"}`,
          }}
        >
          {item.merge_candidate ? "建议合并" : "仅提示"}
        </span>
      </span>
      {item.suggestion ? (
        <span className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
          建议保留 {item.suggestion.keep} · 回收 {item.suggestion.drop}（{item.suggestion.reason}）
        </span>
      ) : (
        <span className="text-[var(--font-caption)]" style={{ color: "var(--text-tertiary)" }}>
          未到合并阈值：先看看是否真的重复（N-07.1）
        </span>
      )}
      <span className="flex flex-wrap items-center gap-sm">
        <button
          type="button"
          onClick={onDiff}
          className="rounded-[var(--radius-control)] px-sm py-xs text-[var(--font-caption)]"
          style={{ border: "1px solid var(--border)", color: "var(--text-secondary)" }}
          title="双栏只读对比 SKILL.md，不产生任何变更（N-07 GWT③）"
        >
          仅查看差异
        </button>
        {keep && (
          <button
            type="button"
            onClick={() => onDeprecate(keep)}
            className="rounded-[var(--radius-control)] px-sm py-xs text-[var(--font-caption)]"
            style={{ border: "1px solid var(--danger)", color: "var(--danger)" }}
            title={`回收 ${drop} 的 Runtime 副本（canonical 保留，可恢复）`}
          >
            回收 {drop}
          </button>
        )}
      </span>
    </div>
  );
}

/** 双栏只读对比（N-07 GWT③）：两个 Skill 的 SKILL.md 并排看。
 *
 *  刻意不用 Monaco：主线不依赖编辑器（CODEBUDDY 前端约定），一处只读对比不值得
 *  把 Monaco chunk 拉进主路径；纯文本 + 等宽即可满足「看清差异」。
 */
function SimilarDiffDialog({
  a, b, onClose,
}: { a: string; b: string; onClose: () => void }) {
  const left = useQuery({
    queryKey: ["skill-file", a, "SKILL.md"],
    queryFn: () => endpoints.fileRead(a, "SKILL.md"),
  });
  const right = useQuery({
    queryKey: ["skill-file", b, "SKILL.md"],
    queryFn: () => endpoints.fileRead(b, "SKILL.md"),
  });
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: "rgba(15, 23, 41, 0.45)" }}
      role="dialog"
      aria-modal="true"
      aria-label="相似对差异"
    >
      <div
        className="flex max-h-[80vh] w-[860px] flex-col rounded-[var(--radius-card)] border"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        <div className="flex items-center justify-between px-lg pt-lg">
          <h2 className="font-semibold" style={{ fontSize: "var(--font-h2)" }}>
            差异对比（只读）
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="text-[var(--font-caption)]"
            style={{ color: "var(--text-secondary)" }}
          >
            关闭
          </button>
        </div>
        <div className="grid flex-1 grid-cols-2 gap-md overflow-hidden p-lg">
          {[["左", a, left], ["右", b, right]].map(([side, slug, q]) => {
            const query = q as typeof left;
            return (
              <div key={side as string} className="flex min-h-0 flex-col gap-xs">
                <span className="text-[var(--font-caption)]" style={{ color: "var(--text-tertiary)" }}>
                  {slug as string} · SKILL.md
                </span>
                <pre
                  className="min-h-0 flex-1 overflow-auto rounded-[var(--radius-control)] p-sm text-[var(--font-mono)]"
                  style={{ background: "var(--surface-alt)", color: "var(--text-primary)" }}
                >
                  {query.isLoading
                    ? "加载中…"
                    : query.isError
                      ? `读取失败：${query.error instanceof Error ? query.error.message : "未知错误"}`
                      : query.data?.content ?? "（空文件）"}
                </pre>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function LegendItem({ color, text }: { color: string; text: string }) {
  return (
    <span
      className="flex items-center gap-sm text-[var(--font-caption)]"
      style={{ color: "var(--text-secondary)" }}
    >
      <span
        className="inline-block shrink-0 rounded-[3px]"
        style={{ width: 10, height: 10, background: color }}
        aria-hidden
      />
      {text}
    </span>
  );
}
