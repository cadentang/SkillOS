import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints, type GateFinding, type HealthSummary, type LogItem } from "../api/endpoints";
import { ApiError } from "../api/client";
import { LazyEditor } from "../components/MonacoLazy";
import FrontmatterForm from "../components/FrontmatterForm";
import RemovalsConfirmDialog from "../components/RemovalsConfirmDialog";
import { useDaemonStatusCtx } from "../ws/DaemonStatusContext";

/** Skill 详情（FR-2.2.x）。Tab：Files（树 + Monaco 异步 + FrontmatterForm）/
 *  Checks（逐条门禁 + 折旧）/ 时间轴（log + 版本间 diff）。
 *  保存走 base_hash 乐观锁：409 STALE → 提示重新加载，不覆盖（FR-3.13）。 */

type Tab = "files" | "checks" | "timeline";

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
    { key: "files", label: "文件" },
    { key: "checks", label: `检查${findings.length ? ` (${findings.length})` : ""}` },
    { key: "timeline", label: "时间轴" },
  ];

  return (
    <div className="flex flex-col gap-md">
      <div className="flex items-center justify-between">
        <h1 className="font-mono font-semibold" style={{ fontSize: "var(--font-h1)" }}>
          {slug}
        </h1>
        <button
          disabled={!daemon.connected}
          onClick={() => void startDelete()}
          className="rounded-[var(--radius-control)] px-md py-sm text-[var(--font-caption)] disabled:opacity-40"
          style={{ background: "var(--surface)", border: "1px solid var(--danger)", color: "var(--danger)" }}
          title="删除 Skill（P3）：只删 canonical + 索引，runtime 磁盘不动"
        >
          删除 Skill
        </button>
      </div>
      {delState && (
        <p className="text-[var(--font-caption)]" style={{ color: "var(--warning)" }}>
          {delState}
        </p>
      )}

      <div className="flex gap-sm">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className="rounded-[var(--radius-control)] px-md py-sm"
            style={{
              background: tab === t.key ? "var(--primary-soft)" : "var(--surface)",
              color: tab === t.key ? "var(--primary)" : "var(--text-secondary)",
              border: `1px solid ${tab === t.key ? "var(--primary-border)" : "var(--border)"}`,
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* ---------- Files：树 + 编辑器 + FrontmatterForm ---------- */}
      {tab === "files" && (
        <div className="flex gap-md">
          <aside
            className="w-[220px] shrink-0 rounded-[var(--radius-card)] border p-sm"
            style={{ background: "var(--surface)", borderColor: "var(--border)" }}
          >
            <p className="px-xs pb-xs text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
              文件（{tree.data?.tree.length ?? 0}）
            </p>
            <ul className="flex flex-col gap-xs">
              {(tree.data?.tree ?? []).map((f) => (
                <li key={f.path}>
                  <button
                    onClick={() => openFile(f.path)}
                    className="w-full truncate rounded-[var(--radius-tag)] px-xs py-xs text-left font-mono text-[var(--font-mono)]"
                    style={{
                      background: activeFile === f.path ? "var(--primary-soft)" : "transparent",
                      color: activeFile === f.path ? "var(--primary)" : "var(--text-primary)",
                    }}
                    title={f.in_git ? f.path : `${f.path}（未纳管资产）`}
                  >
                    {f.path}
                    {!f.in_git && (
                      <span style={{ color: "var(--warning)" }} title="含未纳管资产（FR-1.0.20）">
                        {" "}
                        ▲
                      </span>
                    )}
                  </button>
                </li>
              ))}
              {tree.isError && (
                <li className="px-xs text-[var(--font-caption)]" style={{ color: "var(--warning)" }}>
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
                  language={activeFile.endsWith(".md") ? "markdown" : activeFile.endsWith(".json") ? "json" : "plaintext"}
                  onChange={setEditorValue}
                />
                <div className="flex items-center gap-md">
                  <button
                    disabled={!daemon.connected}
                    onClick={() => saveFile(editorValue, `web 编辑 ${activeFile}`)}
                    className="rounded-[var(--radius-control)] px-md py-sm font-medium text-[var(--inverse)] disabled:opacity-40"
                    style={{ background: "var(--primary)" }}
                  >
                    保存（base_hash 乐观锁）
                  </button>
                  <button
                    onClick={() =>
                      setShowFileHistory(showFileHistory === activeFile ? null : activeFile)
                    }
                    className="rounded-[var(--radius-control)] px-md py-sm text-[var(--font-caption)]"
                    style={{ background: "var(--surface)", border: "1px solid var(--border)" }}
                  >
                    文件历史
                  </button>
                  {saveState && (
                    <span className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
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

      {/* ---------- Checks：逐条门禁 + 折旧 ---------- */}
      {tab === "checks" && (
        <div
          className="rounded-[var(--radius-card)] border p-md"
          style={{ background: "var(--surface)", borderColor: "var(--border)" }}
        >
          {checks.isError && (
            <p style={{ color: "var(--warning)" }}>daemon 离线，检查不可用</p>
          )}
          {health && (
            <HealthStrip health={health} filter={levelFilter} onFilter={setLevelFilter} />
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
                style={{ borderColor: "var(--border)", background: "var(--surface-alt)" }}
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
            <div className="mt-md border-t pt-md" style={{ borderColor: "var(--divider)" }}>
              <p className="font-medium">折旧度量（FR-1.0.23）</p>
              <p className="mt-xs text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
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

      {/* ---------- Timeline：log + 版本间 diff ---------- */}
      {tab === "timeline" && <Timeline slug={slug ?? ""} items={items} />}

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

/** 健康度条（FR-1.0.24 退化形态：门禁结果计数，无 0-100 分）。
 *  NFR-10.3：点击计数下钻过滤对应级别的 findings；折旧原始值随行展示。
 *  T11：仅展示与排序，禁止参与放行判定（放行只看 has_error）。 */
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
          disabled={lv.count === 0}
          onClick={() => onFilter(filter === lv.key ? "all" : lv.key)}
          className="rounded-[var(--radius-tag)] px-sm py-xs font-mono text-[var(--font-caption)] disabled:opacity-40"
          style={{
            color: filter === lv.key ? "var(--inverse)" : lv.color,
            background: filter === lv.key ? lv.color : "transparent",
            border: `1px solid ${lv.color}`,
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

/** 文件级历史（P4）：--follow 跨改名追踪单文件提交序列。 */
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
      <p className="font-mono text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
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
          <li className="text-[var(--font-caption)]" style={{ color: "var(--warning)" }}>
            历史读取失败（daemon 离线？）
          </li>
        )}
      </ul>
    </div>
  );
}

/** Timeline（P3 时间轴 + P4 回滚/tag）。回滚语义：
 *  - dry_run 预览将消失路径 → RemovalsConfirmDialog 逐条确认（FR-1.0.26 ② / FR-2.4.7）
 *  - 回滚前引擎自动建安全快照、可再回滚（FR-1.0.10）；commit 即溯源（FR-1.0.22）。 */
function Timeline({ slug, items }: { slug: string; items: LogItem[] }) {
  const queryClient = useQueryClient();
  const [diff, setDiff] = useState<string | null>(null);
  const [range, setRange] = useState<string | null>(null);
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

  async function showDiff(base: string, head: string) {
    setRange(`${base.slice(0, 8)}… → ${head.slice(0, 8)}…`);
    try {
      const data = await endpoints.skillDiff(slug, base, head);
      setDiff(data.unified || "（无文本差异）");
    } catch (err) {
      setDiff(`diff 获取失败：${err instanceof Error ? err.message : err}`);
    }
  }

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
        setRbTarget({ sha, removals: preview.removals }); // 有人工确认流程
      } else {
        setRbTarget({ sha, removals: [] }); // 无文件消失 → 仍走弹窗（展示变更摘要）
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
        slug, rbTarget.sha, false, rbTarget.removals,
      );
      setRbTarget(null);
      if (result.ok) {
        setRbState(`已回滚到 ${rbTarget.sha.slice(0, 8)}（新 commit ${result.commit?.slice(0, 8)}，快照已存）`);
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
        className="w-[420px] shrink-0 rounded-[var(--radius-card)] border p-md"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        <h2 className="font-medium" style={{ fontSize: "var(--font-h2)" }}>
          变更时间轴
        </h2>
        {rbState && (
          <p className="mt-xs text-[var(--font-caption)]" style={{ color: "var(--warning)" }}>
            {rbState}
          </p>
        )}
        <ol className="mt-md flex flex-col">
          {items.map((c, i) => (
            <li key={c.sha} className="flex gap-md">
              <div className="flex flex-col items-center">
                <span
                  className="mt-1 inline-block h-2 w-2 rounded-full"
                  style={{ background: i === 0 ? "var(--primary)" : "var(--border)" }}
                />
                {i < items.length - 1 && (
                  <span className="w-px flex-1" style={{ background: "var(--divider)" }} />
                )}
              </div>
              <div className="pb-lg">
                <p className="font-medium">
                  {c.message.split("\n")[0]}
                  {tagBySha.has(c.sha) && (
                    <span
                      className="ml-xs rounded-[var(--radius-tag)] px-xs font-mono text-[var(--font-caption)]"
                      style={{ background: "var(--primary-soft)", color: "var(--primary)" }}
                      title={`tag：${tagBySha.get(c.sha)}`}
                    >
                      {tagBySha.get(c.sha)}
                    </span>
                  )}
                </p>
                <p className="mt-xs font-mono text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
                  {/* FR-2.2.7：谁在什么时候改了什么 */}
                  {c.author ? `${c.author} · ` : ""}
                  {c.at.slice(0, 16).replace("T", " ")} · {c.sha.slice(0, 10)}
                </p>
                <div className="mt-xs flex gap-md">
                  {i < items.length - 1 && (
                    <button
                      onClick={() => showDiff(items[i + 1].sha, c.sha)}
                      className="text-[var(--font-caption)]"
                      style={{ color: "var(--primary)" }}
                    >
                      与上一版 diff
                    </button>
                  )}
                  {i > 0 && (
                    <button
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
            <li className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
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
          <span className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
            为当前版本打 tag
          </span>
          <input
            value={tagName}
            onChange={(e) => setTagName(e.target.value)}
            placeholder="v1.0"
            className="rounded-[var(--radius-control)] border px-sm py-xs font-mono text-[var(--font-mono)]"
            style={{ borderColor: "var(--border)", background: "var(--surface-alt)", width: 140 }}
          />
          <button
            onClick={createTag}
            className="rounded-[var(--radius-control)] px-sm py-xs text-[var(--font-caption)]"
            style={{ background: "var(--primary-soft)", color: "var(--primary)" }}
          >
            打 tag
          </button>
          {tagState && (
            <span className="text-[var(--font-caption)]" style={{ color: "var(--warning)" }}>
              {tagState}
            </span>
          )}
        </div>

        {diff === null ? (
          <p className="py-lg" style={{ color: "var(--text-secondary)" }}>
            选择时间轴上的「与上一版 diff」查看版本间差异；「回滚到此版」恢复历史状态
          </p>
        ) : (
          <div
            className="rounded-[var(--radius-card)] border p-md"
            style={{ background: "var(--surface)", borderColor: "var(--border)" }}
          >
            <p className="mb-sm font-mono text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
              {range}
            </p>
            <pre
              className="overflow-x-auto rounded-[var(--radius-control)] p-sm font-mono text-[var(--font-mono)]"
              style={{ background: "var(--surface-alt)", maxHeight: "60vh", overflowY: "auto" }}
            >
              {diff.split("\n").map((line, i) => (
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
      </section>

      {/* 回滚确认（FR-1.0.26 ② / FR-2.4.7 限高滚动结构） */}
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
