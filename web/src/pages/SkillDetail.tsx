import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints, type GateFinding, type HealthSummary, type LogItem } from "../api/endpoints";
import { ApiError } from "../api/client";
import { LazyEditor } from "../components/MonacoLazy";
import FrontmatterForm from "../components/FrontmatterForm";
import RemovalsConfirmDialog from "../components/RemovalsConfirmDialog";
import { useDaemonStatusCtx } from "../ws/DaemonStatusContext";

/** Skill 详情（FR-2.2.x / 设计稿 app/index.html §04）。
 *
 *  视觉：
 *    .crumbs   Skill 库 / slug
 *    .detail-head：左 name + desc；右徽标 + 删除按钮
 *    .tabs     Files / Diff / Versions / Checks（设计稿四 tab）
 *    Files    ：左侧文件树 + 右侧 FrontmatterForm / Monaco
 *    Diff     ：选 base / head 显示版本间 diff（Versions 的「与上一版 diff」也可跳入）
 *    Versions ：变更时间轴 + 回滚 + 打 tag
 *    Checks   ：HealthStrip + findings + decay
 *
 *  保存走 base_hash 乐观锁：409 STALE → 提示重新加载，不覆盖（FR-3.13）。
 *  删除走 dry_run 预览 → RemovalsConfirmDialog 逐条确认（FR-1.0.26 / FR-2.4.7）。
 */

type Tab = "files" | "diff" | "versions" | "checks";

export default function SkillDetail() {
  const { slug } = useParams<{ slug: string }>();
  const navigate = useNavigate();
  const daemon = useDaemonStatusCtx();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>("files");
  const [activeFile, setActiveFile] = useState<string | null>(null);
  const [showFileHistory, setShowFileHistory] = useState<string | null>(null);
  const [editorValue, setEditorValue] = useState<string>("");
  const [baseHash, setBaseHash] = useState<string>("");
  const [saveState, setSaveState] = useState<string | null>(null);
  const [levelFilter, setLevelFilter] = useState<"all" | GateFinding["level"]>("all");
  /** 删除（P3 CRUD）：dry_run 预览 → RemovalsConfirmDialog 逐条确认（FR-1.0.26 ②） */
  const [delRemovals, setDelRemovals] = useState<string[] | null>(null);
  const [delState, setDelState] = useState<string | null>(null);
  /** Diff tab 选定的 base / head；null = 还未选。
   *  父组件 useEffect 监听变化拉取 diff，DiffView 渲染结果。
   *  VersionsView 的「与上一版 diff」会 setTab("diff") + 写入这两个值。 */
  const [diffBase, setDiffBase] = useState<string | null>(null);
  const [diffHead, setDiffHead] = useState<string | null>(null);
  const [diffText, setDiffText] = useState<string | null>(null);
  const [diffRange, setDiffRange] = useState<string | null>(null);
  const [diffErr, setDiffErr] = useState<string | null>(null);

  const tree = useQuery({
    queryKey: ["tree", slug],
    queryFn: () => endpoints.fileTree(slug!),
    enabled: !!slug,
    retry: false,
  });
  const checks = useQuery({
    queryKey: ["checks", slug],
    queryFn: () => endpoints.skillChecks(slug!),
    enabled: !!slug,
    retry: false,
  });
  const log = useQuery({
    queryKey: ["log", slug],
    queryFn: () => endpoints.skillLog(slug!),
    enabled: !!slug,
    retry: false,
  });

  async function openFile(path: string) {
    if (!slug) return;
    try {
      const data = await endpoints.fileRead(slug, path);
      setActiveFile(path);
      setEditorValue(data.content);
      setBaseHash(data.hash);
      setSaveState(null);
    } catch (err) {
      setSaveState(`读取失败：${err instanceof Error ? err.message : err}`);
    }
  }

  async function saveFile(content: string, message: string) {
    if (!slug || !activeFile) return;
    setSaveState("保存中…");
    try {
      const result = await endpoints.fileWrite(slug, activeFile, content, baseHash, message);
      setBaseHash(result.hash);
      setSaveState(
        result.gate_has_error
          ? `已保存 ${result.commit.slice(0, 8)}（⚠ 门禁 error 未过，push 会被拦截）`
          : `已保存 ${result.commit.slice(0, 8)}`,
      );
      void queryClient.invalidateQueries({ queryKey: ["tree", slug] });
      void queryClient.invalidateQueries({ queryKey: ["checks", slug] });
      void queryClient.invalidateQueries({ queryKey: ["log", slug] });
    } catch (err) {
      if (err instanceof ApiError && err.code === "STALE") {
        setSaveState("内容已被其它入口修改（409 STALE）——点击文件重新加载后再保存");
      } else {
        setSaveState(`保存失败：${err instanceof Error ? err.message : err}`);
      }
    }
  }

  const findings = checks.data?.findings ?? [];
  const decay = checks.data?.decay ?? null;
  const health = checks.data?.health ?? null;
  const items = log.data?.items ?? [];
  const visibleFindings =
    levelFilter === "all" ? findings : findings.filter((f) => f.level === levelFilter);

  /** Diff tab 拉取：base / head 任一变化都重新拉。
   *  与 VersionsView「与上一版 diff」按钮解耦：按钮只 setTab + setBase/Head，
   *  真正的 fetch 统一在此处执行，避免 VersionsView 持有网络副作用。 */
  useEffect(() => {
    if (!slug || !diffBase || !diffHead) return;
    let cancelled = false;
    setDiffErr(null);
    setDiffRange(`${diffBase.slice(0, 8)}… → ${diffHead.slice(0, 8)}…`);
    endpoints
      .skillDiff(slug, diffBase, diffHead)
      .then((data) => {
        if (!cancelled) setDiffText(data.unified || "（无文本差异）");
      })
      .catch((err) => {
        if (!cancelled) {
          setDiffText(null);
          setDiffErr(err instanceof Error ? err.message : String(err));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [slug, diffBase, diffHead]);

  /** 删除 Skill（P3 CRUD）：先 dry_run 预览 → RemovalsConfirmDialog 逐条确认
   *  （FR-1.0.26 ②）→ 确认后带 removals 重发。runtime 磁盘不动（红线 16）。 */
  async function startDelete() {
    if (!slug) return;
    setDelState("预览中…");
    try {
      const preview = await endpoints.skillDelete(slug, true);
      setDelState(null);
      setDelRemovals(preview.removals);
    } catch (err) {
      const apiErr = err instanceof ApiError ? err : null;
      setDelState(`删除预览失败：${apiErr?.message ?? String(err)}`);
    }
  }

  async function executeDelete() {
    if (!slug) return;
    const removals = delRemovals ?? [];
    setDelRemovals(null);
    setDelState("删除中…");
    try {
      await endpoints.skillDelete(slug, false, removals);
      void queryClient.invalidateQueries({ queryKey: ["drift"] });
      void queryClient.invalidateQueries({ queryKey: ["gate"] });
      navigate("/");
    } catch (err) {
      const apiErr = err instanceof ApiError ? err : null;
      setDelState(`删除失败：${apiErr?.message ?? String(err)}`);
    }
  }

  const tabs: { key: Tab; label: string }[] = [
    { key: "files", label: "Files" },
    { key: "diff", label: "Diff" },
    { key: "versions", label: "Versions" },
    { key: "checks", label: `Checks${findings.length ? ` (${findings.length})` : ""}` },
  ];

  return (
    <div className="flex flex-col gap-md">
      {/* ────── 面包屑（设计稿 .crumbs） ────── */}
      <nav
        className="flex items-center gap-sm text-[13px]"
        style={{ color: "var(--text-tertiary)" }}
      >
        <Link to="/" style={{ color: "var(--text-tertiary)" }}>
          Skill 库
        </Link>
        <span style={{ color: "var(--text-tertiary)" }}>/</span>
        <span className="font-mono font-medium" style={{ color: "var(--text-primary)" }}>
          {slug}
        </span>
      </nav>

      {/* ────── detail-head：name + desc + 徽标 + 删除 ────── */}
      <div className="flex items-center justify-between gap-md">
        <div className="flex min-w-0 flex-col gap-xs">
          <h1
            className="font-mono font-bold tracking-tight"
            style={{ fontSize: "var(--font-h1)", letterSpacing: "-.3px" }}
          >
            {slug}
          </h1>
          <span
            className="text-[13px]"
            style={{ color: "var(--text-secondary)" }}
          >
            {checks.data?.findings?.length === 0
              ? "Skill 包 — 通过门禁检查"
              : "Skill 包 — 当前版本"}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-sm">
          {/* 两类徽标（FR-1.0.19 / FR-1.0.20 简化渲染 —— 数据接入由后端决定） */}
          <span
            className="inline-flex items-center gap-xs rounded-[var(--radius-tag)] border px-sm py-xs text-[var(--font-caption)]"
            style={{
              background: "var(--surface)",
              borderColor: "var(--border)",
              color: "var(--text-secondary)",
            }}
            title="含未纳管资产（FR-1.0.20）：存在超阈值大文件，不参与分发"
          >
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden>
              <path d="M6 1.2L11 10.4H1L6 1.2Z" stroke="#5C6373" strokeWidth="1.2" strokeLinejoin="round" />
              <path d="M6 4.8V7" stroke="#5C6373" strokeWidth="1.2" strokeLinecap="round" />
              <circle cx="6" cy="8.8" r="0.7" fill="#5C6373" />
            </svg>
            含未纳管资产
          </span>
          <span
            className="inline-flex items-center gap-xs rounded-[var(--radius-tag)] border px-sm py-xs text-[var(--font-caption)]"
            style={{
              background: "var(--surface)",
              borderColor: "var(--badge-adapter)",
              color: "var(--badge-adapter)",
            }}
            title="模板已升级（FR-1.0.19）"
          >
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden>
              <path d="M6 10.5V1.5M2.5 5L6 1.5L9.5 5" stroke="#0EA5E9" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            模板已升级
          </span>
          <button
            type="button"
            disabled={!daemon.connected}
            onClick={() => void startDelete()}
            className="rounded-[var(--radius-control)] border px-md py-sm text-[13px] disabled:opacity-40"
            style={{
              background: "var(--surface)",
              borderColor: "var(--danger)",
              color: "var(--danger)",
            }}
            title="删除 Skill（P3）"
          >
            删除 Skill
          </button>
        </div>
      </div>

      {delState && (
        <p
          className="text-[var(--font-caption)]"
          style={{ color: "var(--warning)" }}
        >
          {delState}
        </p>
      )}

      {/* ────── Tabs（设计稿 .tabs：active 加 2px primary line） ────── */}
      <div className="flex flex-col">
        <div className="flex items-end gap-lg" style={{ marginBottom: -1 }}>
          {tabs.map((t) => {
            const active = tab === t.key;
            return (
              <button
                key={t.key}
                type="button"
                onClick={() => setTab(t.key)}
                className="flex flex-col items-center gap-sm pb-sm transition-colors"
                style={{
                  fontSize: "var(--font-body)",
                  fontWeight: active ? 600 : 500,
                  color: active ? "var(--primary)" : "var(--text-secondary)",
                }}
              >
                <span>{t.label}</span>
                <span
                  className="h-[2px] w-full rounded-sm"
                  style={{ background: active ? "var(--primary)" : "transparent" }}
                />
              </button>
            );
          })}
        </div>
        <div
          className="h-px w-full"
          style={{ background: "var(--divider)" }}
        />
      </div>

      {/* ───────── Files：树 + 编辑器 + FrontmatterForm ───────── */}
      {tab === "files" && (
        <div className="flex gap-md">
          <aside
            className="flex w-[220px] shrink-0 flex-col gap-xs p-sm"
            style={{
              background: "var(--surface)",
              border: "1px solid var(--border)",
              borderRadius: "var(--radius-card)",
            }}
          >
            <p
              className="px-xs pb-xs text-[var(--font-caption)]"
              style={{ color: "var(--text-secondary)" }}
            >
              文件树（{tree.data?.tree.length ?? 0}）
            </p>
            <ul className="flex flex-col gap-xs">
              {(tree.data?.tree ?? []).map((f) => (
                <li key={f.path}>
                  <button
                    type="button"
                    onClick={() => openFile(f.path)}
                    className="w-full truncate rounded-[var(--radius-tag)] px-xs py-xs text-left font-mono text-[var(--font-mono)]"
                    style={{
                      background:
                        activeFile === f.path ? "var(--primary-soft)" : "transparent",
                      color:
                        activeFile === f.path
                          ? "var(--primary)"
                          : "var(--text-primary)",
                    }}
                    title={f.in_git ? f.path : `${f.path}（未纳管资产）`}
                  >
                    {f.path}
                    {!f.in_git && (
                      <span style={{ color: "var(--warning)" }} title="含未纳管资产">
                        {" "}▲
                      </span>
                    )}
                  </button>
                </li>
              ))}
              {tree.isError && (
                <li
                  className="px-xs text-[var(--font-caption)]"
                  style={{ color: "var(--warning)" }}
                >
                  daemon 离线，文件树不可用
                </li>
              )}
            </ul>
          </aside>

          <div className="min-w-0 flex-1">
            {!activeFile && (
              <p className="py-lg" style={{ color: "var(--text-secondary)" }}>
                选择左侧文件查看 / 编辑（保存需 daemon 在线）
              </p>
            )}
            {activeFile === "SKILL.md" && (
              <FrontmatterForm
                initial={editorValue}
                disabled={!daemon.connected}
                onSave={(next) => {
                  setEditorValue(next);
                  void saveFile(next, "编辑 frontmatter");
                }}
              />
            )}
            {activeFile && (
              <div className="mt-md flex flex-col gap-sm">
                <LazyEditor
                  value={editorValue}
                  language={
                    activeFile.endsWith(".md")
                      ? "markdown"
                      : activeFile.endsWith(".json")
                        ? "json"
                        : "plaintext"
                  }
                  onChange={setEditorValue}
                />
                <div className="flex items-center gap-md">
                  <button
                    type="button"
                    disabled={!daemon.connected}
                    onClick={() => saveFile(editorValue, `web 编辑 ${activeFile}`)}
                    className="rounded-[var(--radius-control)] px-md py-sm text-[13px] font-medium text-[var(--inverse)] disabled:opacity-40"
                    style={{ background: "var(--primary)" }}
                  >
                    保存（base_hash 乐观锁）
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      setShowFileHistory(showFileHistory === activeFile ? null : activeFile)
                    }
                    className="rounded-[var(--radius-control)] border px-sm py-xs text-[var(--font-caption)]"
                    style={{
                      background: "var(--surface)",
                      borderColor: "var(--border)",
                    }}
                  >
                    文件历史
                  </button>
                  {saveState && (
                    <span
                      className="text-[var(--font-caption)]"
                      style={{ color: "var(--text-secondary)" }}
                    >
                      {saveState}
                    </span>
                  )}
                </div>
                {showFileHistory && (
                  <FileHistoryPanel slug={slug ?? ""} path={showFileHistory} />
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {/* ───────── Checks：HealthStrip + findings + decay ───────── */}
      {tab === "checks" && (
        <div
          className="rounded-[var(--radius-card)] border p-md"
          style={{ background: "var(--surface)", borderColor: "var(--border)" }}
        >
          {checks.isError && (
            <p style={{ color: "var(--warning)" }}>daemon 离线，检查不可用</p>
          )}
          {health && (
            <HealthStrip
              health={health}
              filter={levelFilter}
              onFilter={setLevelFilter}
            />
          )}
          {checks.data && checks.data.findings.length === 0 && (
            <p style={{ color: "var(--success)" }}>全部规则通过，无发现</p>
          )}
          {levelFilter !== "all" && findings.length > 0 && (
            <p
              className="pb-xs text-[var(--font-caption)]"
              style={{ color: "var(--text-secondary)" }}
            >
              下钻：仅显示 {levelFilter} 级（{visibleFindings.length} 条）·{" "}
              <button
                type="button"
                className="underline"
                onClick={() => setLevelFilter("all")}
                style={{ color: "var(--primary)" }}
              >
                显示全部
              </button>
            </p>
          )}
          <ul className="flex flex-col gap-xs">
            {visibleFindings.map((f: GateFinding, i) => (
              <li
                key={`${f.rule}-${i}`}
                className="rounded-[var(--radius-control)] border px-sm py-sm"
                style={{
                  borderColor: "var(--border)",
                  background: "var(--surface-alt)",
                }}
              >
                <span
                  className="mr-sm rounded-[var(--radius-tag)] px-xs py-xs text-[var(--font-caption)] text-[var(--inverse)]"
                  style={{
                    background:
                      f.level === "error"
                        ? "var(--danger)"
                        : f.level === "warn"
                          ? "var(--warning)"
                          : "var(--info)",
                  }}
                >
                  {f.level}
                </span>
                <span className="font-mono text-[var(--font-mono)]">{f.rule}</span>
                <span className="ml-sm" style={{ color: "var(--text-secondary)" }}>
                  {f.message}
                </span>
              </li>
            ))}
          </ul>
          {decay && (
            <div
              className="mt-md border-t pt-md"
              style={{ borderColor: "var(--divider)" }}
            >
              <p className="font-medium">折旧度量（FR-1.0.23）</p>
              <p
                className="mt-xs text-[var(--font-caption)]"
                style={{ color: "var(--text-secondary)" }}
              >
                距上次更新 {decay.staleness_days ?? "—"} 天
                {decay.dependency_broken.length > 0 && (
                  <>
                    {" · "}
                    <span style={{ color: "var(--danger)" }}>
                      失效依赖：{decay.dependency_broken.join(", ")}
                    </span>
                  </>
                )}
              </p>
            </div>
          )}
        </div>
      )}

      {/* ───────── Diff：版本间差异（设计稿 .diff） ───────── */}
      {tab === "diff" && (
        <DiffView
          slug={slug ?? ""}
          items={items}
          base={diffBase}
          head={diffHead}
          text={diffText}
          range={diffRange}
          err={diffErr}
          onPickBase={setDiffBase}
          onPickHead={setDiffHead}
        />
      )}

      {/* ───────── Versions：变更时间轴 + 回滚 + 打 tag（设计稿 .versions） ───────── */}
      {tab === "versions" && (
        <VersionsView
          slug={slug ?? ""}
          items={items}
          onDiffWithPrev={(base, head) => {
            setDiffBase(base);
            setDiffHead(head);
            setTab("diff");
          }}
        />
      )}

      {/* 删除确认（FR-1.0.26 ② / FR-2.4.7 限高滚动结构） */}
      {delRemovals && delRemovals.length > 0 && (
        <RemovalsConfirmDialog
          paths={delRemovals}
          onCancel={() => setDelRemovals(null)}
          onConfirm={executeDelete}
        />
      )}
    </div>
  );
}

/** 健康度条（FR-1.0.24 退化形态）。点击计数下钻过滤对应级别 findings。 */
function HealthStrip({
  health,
  filter,
  onFilter,
}: {
  health: HealthSummary;
  filter: "all" | GateFinding["level"];
  onFilter: (next: "all" | GateFinding["level"]) => void;
}) {
  const levels: { key: GateFinding["level"]; count: number; color: string }[] = [
    { key: "error", count: health.error_count, color: "var(--danger)" },
    { key: "warn", count: health.warn_count, color: "var(--warning)" },
    { key: "info", count: health.info_count, color: "var(--info)" },
  ];
  return (
    <div
      className="mb-md flex flex-wrap items-center gap-sm border-b pb-md"
      style={{ borderColor: "var(--divider)" }}
    >
      <span className="font-medium">健康度</span>
      {levels.map((lv) => (
        <button
          key={lv.key}
          type="button"
          disabled={lv.count === 0}
          onClick={() => onFilter(filter === lv.key ? "all" : lv.key)}
          className="rounded-[var(--radius-tag)] border px-sm py-xs font-mono text-[var(--font-caption)] disabled:opacity-40"
          style={{
            color: filter === lv.key ? "var(--inverse)" : lv.color,
            background: filter === lv.key ? lv.color : "transparent",
            borderColor: lv.color,
          }}
          title={`点击下钻：仅显示 ${lv.key} 级发现`}
        >
          {lv.key} {lv.count}
        </button>
      ))}
      <span
        className="ml-sm text-[var(--font-caption)]"
        style={{ color: "var(--text-secondary)" }}
      >
        距上次更新 {health.staleness_days ?? "—"} 天 · 失效依赖{" "}
        {health.dependency_broken_count}
      </span>
    </div>
  );
}

/** 文件级历史（P4）。 */
function FileHistoryPanel({ slug, path }: { slug: string; path: string }) {
  const hist = useQuery({
    queryKey: ["file-history", slug, path],
    queryFn: () => endpoints.skillFileHistory(slug, path),
    retry: false,
  });
  return (
    <div
      className="rounded-[var(--radius-card)] border p-sm"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <p
        className="font-mono text-[var(--font-caption)]"
        style={{ color: "var(--text-secondary)" }}
      >
        {path} 的提交历史
      </p>
      <ul
        className="mt-xs flex flex-col gap-xs overflow-y-auto"
        style={{ maxHeight: "30vh" }}
      >
        {(hist.data?.items ?? []).map((h) => (
          <li
            key={h.sha}
            className="rounded-[var(--radius-tag)] px-sm py-xs font-mono text-[var(--font-mono)]"
            style={{ background: "var(--surface-alt)" }}
          >
            {h.at.slice(0, 16).replace("T", " ")} · {h.sha.slice(0, 8)} ·{" "}
            {h.author ?? "unknown"} — {h.message.split("\n")[0]}
          </li>
        ))}
        {hist.isError && (
          <li
            className="text-[var(--font-caption)]"
            style={{ color: "var(--warning)" }}
          >
            历史读取失败（daemon 离线？）
          </li>
        )}
      </ul>
    </div>
  );
}

/** Diff tab：版本间差异（设计稿 .diff）。
 *  base / head 由父组件持有；fetch 也在父组件 useEffect 跑完。
 *  本组件是受控的：选 commit → onPickBase/Head → 父组件 useEffect 拉 → 文本回灌。 */
function DiffView({
  items,
  base,
  head,
  text,
  range,
  err,
  onPickBase,
  onPickHead,
}: {
  slug: string;
  items: LogItem[];
  base: string | null;
  head: string | null;
  text: string | null;
  range: string | null;
  err: string | null;
  onPickBase: (sha: string) => void;
  onPickHead: (sha: string) => void;
}) {
  return (
    <div className="flex flex-col gap-md">
      <div
        className="flex flex-wrap items-center gap-sm rounded-[var(--radius-card)] border p-sm"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        <span
          className="text-[var(--font-caption)]"
          style={{ color: "var(--text-secondary)" }}
        >
          基准
        </span>
        <select
          value={base ?? ""}
          onChange={(e) => onPickBase(e.target.value)}
          className="rounded-[var(--radius-control)] border px-sm py-xs font-mono text-[var(--font-mono)]"
          style={{
            borderColor: "var(--border)",
            background: "var(--surface-alt)",
            minWidth: 220,
          }}
        >
          <option value="">（选择 commit）</option>
          {items.map((c) => (
            <option key={c.sha} value={c.sha}>
              {c.sha.slice(0, 8)} · {c.at.slice(0, 10)} · {c.message.split("\n")[0]}
            </option>
          ))}
        </select>
        <span
          className="text-[var(--font-caption)]"
          style={{ color: "var(--text-tertiary)" }}
        >
          →
        </span>
        <span
          className="text-[var(--font-caption)]"
          style={{ color: "var(--text-secondary)" }}
        >
          目标
        </span>
        <select
          value={head ?? ""}
          onChange={(e) => onPickHead(e.target.value)}
          className="rounded-[var(--radius-control)] border px-sm py-xs font-mono text-[var(--font-mono)]"
          style={{
            borderColor: "var(--border)",
            background: "var(--surface-alt)",
            minWidth: 220,
          }}
        >
          <option value="">（选择 commit）</option>
          {items.map((c) => (
            <option key={c.sha} value={c.sha}>
              {c.sha.slice(0, 8)} · {c.at.slice(0, 10)} · {c.message.split("\n")[0]}
            </option>
          ))}
        </select>
        {range && (
          <span
            className="font-mono text-[var(--font-caption)]"
            style={{ color: "var(--text-tertiary)" }}
          >
            {range}
          </span>
        )}
      </div>

      {!base || !head ? (
        <p className="py-lg" style={{ color: "var(--text-secondary)" }}>
          在 Versions tab 选一个 commit 点「与上一版 diff」，或在此选 base / head 查看版本间差异
        </p>
      ) : err ? (
        <p style={{ color: "var(--danger)" }}>diff 获取失败：{err}</p>
      ) : text === null ? (
        <p style={{ color: "var(--text-secondary)" }}>拉取中…</p>
      ) : (
        <div
          className="rounded-[var(--radius-card)] border p-md"
          style={{ background: "var(--surface)", borderColor: "var(--border)" }}
        >
          <pre
            className="overflow-x-auto rounded-[var(--radius-control)] p-sm font-mono text-[var(--font-mono)]"
            style={{
              background: "var(--surface-alt)",
              maxHeight: "60vh",
              overflowY: "auto",
            }}
          >
            {text.split("\n").map((line, i) => (
              <div
                key={i}
                style={{
                  color: line.startsWith("+")
                    ? "var(--success)"
                    : line.startsWith("-")
                      ? "var(--danger)"
                      : "var(--text-primary)",
                }}
              >
                {line || " "}
              </div>
            ))}
          </pre>
        </div>
      )}
    </div>
  );
}

/** Versions tab：变更时间轴 + 回滚 + 打 tag（设计稿 .versions）。
 *  网络副作用：startRollback / executeRollback / createTag 三处；
 *  diff 业务已被父组件收走（base/head 写父 state，fetch 走父 useEffect）。 */
function VersionsView({
  slug,
  items,
  onDiffWithPrev,
}: {
  slug: string;
  items: LogItem[];
  onDiffWithPrev: (base: string, head: string) => void;
}) {
  const queryClient = useQueryClient();
  const [rbTarget, setRbTarget] = useState<{ sha: string; removals: string[] } | null>(null);
  const [rbState, setRbState] = useState<string | null>(null);
  const [tagName, setTagName] = useState("");
  const [tagState, setTagState] = useState<string | null>(null);

  const tags = useQuery({
    queryKey: ["tags", slug],
    queryFn: () => endpoints.skillTags(slug),
    retry: false,
  });
  const tagBySha = new Map((tags.data?.items ?? []).map((t) => [t.sha, t.name]));

  async function startRollback(sha: string) {
    setRbState("预览中…");
    try {
      const preview = await endpoints.skillRollback(slug, sha, true);
      if (preview.noop) {
        setRbState("当前已是该版本内容（无差异）");
        return;
      }
      setRbState(null);
      if (preview.removals.length > 0) {
        setRbTarget({ sha, removals: preview.removals });
      } else {
        setRbTarget({ sha, removals: [] });
      }
    } catch (err) {
      setRbState(`预览失败：${err instanceof Error ? err.message : err}`);
    }
  }

  async function executeRollback() {
    if (!rbTarget) return;
    setRbState("回滚中…");
    try {
      const result = await endpoints.skillRollback(
        slug,
        rbTarget.sha,
        false,
        rbTarget.removals,
      );
      setRbTarget(null);
      if (result.ok) {
        setRbState(
          `已回滚到 ${rbTarget.sha.slice(0, 8)}（新 commit ${result.commit?.slice(0, 8)}，快照已存）`,
        );
        void queryClient.invalidateQueries({ queryKey: ["log", slug] });
        void queryClient.invalidateQueries({ queryKey: ["tree", slug] });
        void queryClient.invalidateQueries({ queryKey: ["checks", slug] });
      } else {
        setRbState(`回滚失败：${result.error?.message ?? "未知错误"}`);
      }
    } catch (err) {
      setRbTarget(null);
      setRbState(`回滚失败：${err instanceof Error ? err.message : err}`);
    }
  }

  async function createTag() {
    if (!tagName.trim()) return;
    setTagState("创建中…");
    try {
      await endpoints.skillTagCreate(slug, tagName.trim());
      setTagState(`已打 tag ${tagName.trim()}`);
      setTagName("");
      void queryClient.invalidateQueries({ queryKey: ["tags", slug] });
      void queryClient.invalidateQueries({ queryKey: ["log", slug] });
    } catch (err) {
      setTagState(`创建失败：${err instanceof Error ? err.message : err}`);
    }
  }

  return (
    <div className="flex gap-md">
      <section
        className="flex w-[420px] shrink-0 flex-col gap-md rounded-[var(--radius-card)] border p-md"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        <h2 className="font-medium" style={{ fontSize: "var(--font-h2)" }}>
          变更时间轴
        </h2>
        {rbState && (
          <p
            className="text-[var(--font-caption)]"
            style={{ color: "var(--warning)" }}
          >
            {rbState}
          </p>
        )}
        <ol className="flex flex-col">
          {items.map((c, i) => (
            <li key={c.sha} className="flex gap-md">
              <div className="flex flex-col items-center">
                <span
                  className="mt-1 inline-block h-2 w-2 rounded-full"
                  style={{
                    background: i === 0 ? "var(--primary)" : "var(--border)",
                  }}
                />
                {i < items.length - 1 && (
                  <span
                    className="w-px flex-1"
                    style={{ background: "var(--divider)" }}
                  />
                )}
              </div>
              <div className="pb-lg">
                <p className="font-medium">
                  {c.message.split("\n")[0]}
                  {tagBySha.has(c.sha) && (
                    <span
                      className="ml-xs rounded-[var(--radius-tag)] px-xs font-mono text-[var(--font-caption)]"
                      style={{
                        background: "var(--primary-soft)",
                        color: "var(--primary)",
                      }}
                      title={`tag：${tagBySha.get(c.sha)}`}
                    >
                      {tagBySha.get(c.sha)}
                    </span>
                  )}
                </p>
                <p
                  className="mt-xs font-mono text-[var(--font-caption)]"
                  style={{ color: "var(--text-secondary)" }}
                >
                  {/* FR-2.2.7：谁在什么时候改了什么 */}
                  {c.author ? `${c.author} · ` : ""}
                  {c.at.slice(0, 16).replace("T", " ")} · {c.sha.slice(0, 10)}
                </p>
                <div className="mt-xs flex gap-md">
                  {i < items.length - 1 && (
                    <button
                      type="button"
                      onClick={() => onDiffWithPrev(items[i + 1].sha, c.sha)}
                      className="text-[var(--font-caption)]"
                      style={{ color: "var(--primary)" }}
                    >
                      与上一版 diff
                    </button>
                  )}
                  {i > 0 && (
                    <button
                      type="button"
                      onClick={() => startRollback(c.sha)}
                      className="text-[var(--font-caption)]"
                      style={{ color: "var(--warning)" }}
                      title="回滚到此版本（前自动建安全快照，可再回滚）"
                    >
                      回滚到此版
                    </button>
                  )}
                </div>
              </div>
            </li>
          ))}
          {items.length === 0 && (
            <li
              className="text-[var(--font-caption)]"
              style={{ color: "var(--text-secondary)" }}
            >
              暂无提交记录
            </li>
          )}
        </ol>
      </section>

      <section className="min-w-0 flex-1">
        {/* 打 tag（P4）：tag 是 repo 级标记，打在当前 HEAD */}
        <div
          className="mb-md flex items-center gap-sm rounded-[var(--radius-card)] border p-sm"
          style={{ background: "var(--surface)", borderColor: "var(--border)" }}
        >
          <span
            className="text-[var(--font-caption)]"
            style={{ color: "var(--text-secondary)" }}
          >
            为当前版本打 tag
          </span>
          <input
            value={tagName}
            onChange={(e) => setTagName(e.target.value)}
            placeholder="v1.0"
            className="rounded-[var(--radius-control)] border px-sm py-xs font-mono text-[var(--font-mono)]"
            style={{
              borderColor: "var(--border)",
              background: "var(--surface-alt)",
              width: 140,
            }}
          />
          <button
            type="button"
            onClick={createTag}
            className="rounded-[var(--radius-control)] px-sm py-xs text-[var(--font-caption)]"
            style={{ background: "var(--primary-soft)", color: "var(--primary)" }}
          >
            打 tag
          </button>
          {tagState && (
            <span
              className="text-[var(--font-caption)]"
              style={{ color: "var(--warning)" }}
            >
              {tagState}
            </span>
          )}
        </div>

        <p className="py-lg" style={{ color: "var(--text-secondary)" }}>
          选择时间轴上的「与上一版 diff」查看版本间差异（自动跳到 Diff tab）；
          「回滚到此版」恢复历史状态（前自动建安全快照，可再回滚找回）
        </p>
      </section>

      {rbTarget && (
        <RemovalsConfirmDialog
          paths={rbTarget.removals}
          onCancel={() => setRbTarget(null)}
          onConfirm={executeRollback}
          title={rbTarget.removals.length > 0 ? "回滚将删除这些文件" : "确认回滚"}
          description={
            rbTarget.removals.length > 0
              ? `回滚到 ${rbTarget.sha.slice(0, 8)} 后，以下 ${rbTarget.removals.length} 个文件将从该 Skill 中消失（目标版本中不存在它们）。回滚前会自动创建安全快照，可再次回滚找回。`
              : `将 ${slug} 回滚到 ${rbTarget.sha.slice(0, 8)}。当前内容会先自动创建安全快照（可再回滚找回），然后恢复目标版本。`
          }
          confirmText={rbTarget.removals.length > 0 ? "确认回滚（删除上述文件）" : "确认回滚"}
        />
      )}
    </div>
  );
}
