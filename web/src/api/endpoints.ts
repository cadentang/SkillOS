/** REST 端点封装（契约手写镜像；gen:api 后可迁 schema.d.ts 类型）。 */

import { api } from "./client";

// ---- 类型（与后端路由一一对应） ----

export interface DriftCell {
  state: string;
  has_untracked_assets?: boolean;
  adapter_outdated?: boolean;
}

export interface DriftRow {
  slug: string;
  state: string; // 按 Skill 聚合（FR-2.1.9），含 PARTIAL
  cells: Record<string, DriftCell>; // runtime_key → cell
}

export interface ManagerStats {
  total_skills: number;
  total_cells: number;
  coverage_pct: number;
  zombie_pct: number;
  health: Record<string, number>;
}

export interface DriftStatus {
  rows: DriftRow[];
  summary: Record<string, number>;
  manager: ManagerStats;
  daemon_online: boolean;
  stale: boolean;
}

export interface PushResultItem {
  skill: string;
  runtime: string | null;
  ok: boolean;
  error?: { code: string; message: string };
  held_back_removals?: string[];
}

export interface PushResponse {
  results: PushResultItem[];
  held_back_removals: string[];
}

export interface GateSummary {
  summary: Record<string, { errors: number; warns: number }>;
}

export interface LogItem {
  sha: string;
  at: string;
  message: string;
  author?: string;
}

/** 单条日志事件（FR-1.1.12 / 04 §6.2.6）。 */
export interface LogEvent {
  ts: string;
  level: string;
  logger: string;
  event: string;
  op_id?: string;
  msg?: string;
  [k: string]: unknown;
}

/** 日志查询结果（FR-1.1.12）。 */
export interface LogsResponse {
  events: LogEvent[];
  skipped: number;
  path: string;
  count?: number;
  missing?: boolean;
}

/** 回滚预览/执行结果（FR-2.2.8 / FR-1.0.26 ②）。 */
export interface RollbackResult {
  ok: boolean;
  noop?: boolean;
  dry_run?: boolean;
  commit?: string;
  target?: string;
  removals: string[];
  changed: string[];
  restored: string[];
  error?: { code: string; message: string };
}

/** 删除预览/执行结果（P3 CRUD / FR-1.0.26 ②）。 */
export interface DeleteResult {
  ok: boolean;
  dry_run?: boolean;
  commit?: string;
  removals: string[];
}

export interface TagItem {
  name: string;
  sha: string;
}

// ---- P6 协作 ----

export interface TeamMember {
  user_id: string;
  role: "owner" | "member" | "viewer";
  email?: string | null;
  name?: string | null;
}

/** CR 五态（E-05.1）：终态 = merged / rejected / closed，不可回跳。 */
export type CRStatus = "open" | "approved" | "rejected" | "merged" | "closed";

/** 迁移事件：审查 approve/reject、处置 merge/close（服务端迁移表是唯一事实源）。 */
export type CREvent = "approve" | "reject" | "merge" | "close";

export interface TeamCR {
  id: string;
  team_id: string;
  skill_id: string;
  title: string;
  body: string | null;
  status: CRStatus;
  author_id: string | null;
  reviewer_id: string | null;
  base_version: string | null;
  target_commit: string | null;
  /** 当前状态允许的事件（服务端按迁移表推导，前端不另抄状态机） */
  next_events?: CREvent[];
  comment_count: number;
  comments?: CRComment[];
  created_at: string | null;
  updated_at: string | null;
}

export interface CRComment {
  id: string;
  cr_id: string;
  author_id: string | null;
  body: string;
  at: string | null;
}

export interface TeamItem {
  id: string;
  name: string;
  owner_id: string;
  role: "owner" | "member" | "viewer";
  created_at: string | null;
}

export interface TeamDetail extends TeamItem {
  members: TeamMember[];
  crs: TeamCR[];
}

export interface TeamDashboard {
  total_skills: number;
  total_cells: number;
  synced_cells: number;
  coverage_pct: number;
  zombie_pct: number;
  health: Record<string, number>;
  contribution: Array<{ user_id: string | null; skill_count: number; version_count: number }>;
  runtimes?: number;
}

export interface FileEntry {
  path: string;
  hash: string;
  size: number;
  in_git: boolean;
}

export interface FileContent {
  content: string;
  hash: string;
}

export interface GateFinding {
  rule: string;
  level: "error" | "warn" | "info";
  message: string;
}

export interface WriteResult {
  commit: string;
  hash: string;
  gate: GateFinding[];
  gate_has_error: boolean;
}

export interface DecayMetrics {
  slug: string;
  staleness_days: number | null;
  dependency_broken: string[];
  /** E-08.3：ok / degraded / unknown（无上游记录或连续失败 ⇒ unknown，不猜） */
  staleness?: "ok" | "degraded" | "unknown";
  dependency_broken_status?: "ok" | "degraded" | "unknown";
  upstream_divergence?: "ok" | "degraded" | "unknown";
}

/** 健康度（FR-1.0.24 退化形态：门禁结果计数，无 0-100 分）。
 *  T11：仅展示/排序用，禁止参与放行判定（放行只看 has_error）。 */
export interface HealthSummary {
  error_count: number;
  warn_count: number;
  info_count: number;
  staleness_days: number | null;
  dependency_broken_count: number;
}

// ---- P3 CONFLICT 四选一（FR-2.2.6 / T22 / RED LINE 12）----

/** 决策枚举：keep_local / keep_remote / keep_both / defer。
 *  keep_both 仅 Web 人工路径（MCP 工具签名故意不暴露，test_conflicts_p3 红线守护）。*/
export type ConflictDecision =
  | "keep_local"
  | "keep_remote"
  | "keep_both"
  | "defer";

export interface ResolveConflictResult {
  ok: boolean;
  noop?: boolean;
  decision: ConflictDecision;
  snapshot?: string | null;
  by?: string;
  commit?: string;
  changed?: string[];
  added?: string[];
  removed?: string[];
  kept_local_files?: string[];
}

// ---------------------------------------------------------------- 规则面板（N-03 / N-08）

/** 一条生效中的自定义规则（`rule_engine.as_dict`，不含编译对象）。 */
export interface CustomRuleItem {
  rule_id: string;
  level: "error" | "warn" | "info";
  target: "description" | "frontmatter" | "file_content" | "file_list";
  pattern: string;
  flags: string[];
  scope_paths: string[];
  scope_exclude: string[];
  message: string | null;
  category: string | null;
  source: string;
}

/** 加载失败的规则：原因直出，不静默丢弃（N-03.4）。 */
export interface InvalidRuleItem {
  rule_id: string;
  reason: string;
  source: string;
}

/** ignore 白名单条目：带理由才可审计（N-08.2）。 */
export interface IgnoreRuleItem {
  rule_id: string;
  paths: string[];
  reason: string;
}

export interface RulesPanel {
  ruleset_hash: string;
  files: string[];
  rules: CustomRuleItem[];
  invalid: InvalidRuleItem[];
  ignores: IgnoreRuleItem[];
}

// ---------------------------------------------------------------- 相似对（E-04 / N-07）

/** ≥0.9 才有的合并建议；keep 方保留、drop 方回收 —— 永不自动执行（N-07.2）。 */
export interface SimilaritySuggestion {
  keep: string;
  drop: string;
  reason: string;
}

export interface SimilarityItem {
  a: string;
  b: string;
  score: number;
  method: string;
  merge_candidate: boolean;
  suggestion: SimilaritySuggestion | null;
}

export interface SimilarityRunResult {
  pairs: number;
  added: number;
  skills: number;
  threshold: number;
}

/** 回收落选方（N-07.3）：dry_run 只列 removals；真跑要带 confirm_removals。 */
export interface ResolveResult {
  dry_run?: boolean;
  slug?: string;
  removals?: string[];
  skipped_runtimes?: string[];
  deprecated?: boolean;
  backup?: string;
}

/** 已弃用的 Skill（N-07.5）：带 id 以便直接复用既有 push 路由恢复。 */
export interface DeprecatedSkill {
  slug: string;
  id: string;
}

// ---- 调用 ----

export const endpoints = {
  register: (email: string, password: string, name?: string) =>
    api.post<{ id: string; email: string }>("/api/auth/register", {
      email,
      password,
      name,
    }),
  login: (email: string, password: string) =>
    api.post<{ token: string; user: { id: string; email: string } }>(
      "/api/auth/login",
      { email, password },
    ),
  me: () => api.get<{ id: string; email: string; name: string | null }>("/api/auth/me"),
  driftStatus: () => api.get<DriftStatus>("/api/sync/status?live=1"),
  push: (skillIds: string[], confirmRemovals?: string[]) =>
    api.post<PushResponse>("/api/sync/push", {
      skill_ids: skillIds,
      confirm_removals: confirmRemovals,
    }),
  gateSummary: () => api.post<GateSummary>("/api/sync/gate-summary"),

  // ---- P6 协作 ----
  listTeams: () => api.get<{ items: TeamItem[] }>("/api/teams"),
  createTeam: (name: string) => api.post<TeamDetail>("/api/teams", { name }),
  teamDetail: (id: string) => api.get<TeamDetail>(`/api/teams/${id}`),
  renameTeam: (id: string, name: string) => api.patch<TeamDetail>(`/api/teams/${id}`, { name }),
  addMember: (teamId: string, email: string, role: "member" | "viewer" | "owner") =>
    api.post<TeamMember>(`/api/teams/${teamId}/members`, { email, role }),
  changeRole: (teamId: string, userId: string, role: "member" | "viewer" | "owner") =>
    api.patch<TeamMember>(`/api/teams/${teamId}/members/${userId}`, { email: "", role }),
  removeMember: (teamId: string, userId: string) =>
    api.delete<{ ok: boolean; data: { removed: boolean }; error: unknown }>(
      `/api/teams/${teamId}/members/${userId}`,
    ),
  attachSkill: (teamId: string, slug: string) =>
    api.post<{ ok: boolean; data: { skill_id: string; team_id: string; visibility: string } }>(
      `/api/teams/${teamId}/skills`, { skill_slug: slug },
    ),
  detachSkill: (teamId: string, slug: string) =>
    api.delete<{ ok: boolean }>(`/api/teams/${teamId}/skills/${slug}`),
  teamSkills: (teamId: string) =>
    api.get<{ items: Array<{ id: string; slug: string; name: string; visibility: string }> }>(
      `/api/teams/${teamId}/skills`,
    ),
  createCR: (
    teamId: string, skillSlug: string, title: string,
    body?: string, baseVersion?: string, targetCommit?: string,
  ) => api.post<{ ok: boolean; data: TeamCR }>(
    `/api/teams/${teamId}/change-requests`,
    { skill_slug: skillSlug, title, body, base_version: baseVersion, target_commit: targetCommit },
  ),
  listCRs: (teamId: string, status?: string) =>
    api.get<{ items: TeamCR[] }>(
      `/api/teams/${teamId}/change-requests${status ? `?status=${status}` : ""}`,
    ),
  getCR: (teamId: string, crId: string) =>
    api.get<TeamCR>(`/api/teams/${teamId}/change-requests/${crId}`),
  addCRComment: (teamId: string, crId: string, body: string) =>
    api.post<CRComment>(
      `/api/teams/${teamId}/change-requests/${crId}/comments`, { body },
    ),
  /** 审查（E-05.1）：approve/reject/close；reject 必须带 reason。 */
  reviewCR: (
    teamId: string, crId: string, action: "approve" | "reject" | "close", reason?: string,
  ) => api.post<TeamCR>(
    `/api/teams/${teamId}/change-requests/${crId}/review`,
    { action, reason: reason ?? null },
  ),
  /** 合并（E-05.2/3）：服务端跑门禁 + STALE 比对；`baseHash` 缺省用 CR 的 target_commit。 */
  mergeCR: (teamId: string, crId: string, baseHash?: string) =>
    api.post<TeamCR>(
      `/api/teams/${teamId}/change-requests/${crId}/merge`,
      { base_hash: baseHash ?? null },
    ),
  teamDashboard: (teamId: string) =>
    api.get<TeamDashboard>(`/api/teams/${teamId}/dashboard`),
  skillLog: (slug: string, limit = 50) =>
    api.get<{ items: LogItem[] }>(`/api/skills/${encodeURIComponent(slug)}/log?limit=${limit}`),
  skillDiff: (slug: string, base: string, head: string) =>
    api.get<{ unified: string }>(
      `/api/skills/${encodeURIComponent(slug)}/diff?base=${encodeURIComponent(base)}&head=${encodeURIComponent(head)}`,
    ),
  skillRollback: (slug: string, ref: string, dryRun: boolean, confirmRemovals?: string[]) =>
    api.post<RollbackResult>(`/api/skills/${encodeURIComponent(slug)}/rollback`, {
      ref,
      dry_run: dryRun,
      confirm_removals: confirmRemovals ?? [],
    }),
  skillFileHistory: (slug: string, path: string, limit = 50) =>
    api.get<{ items: LogItem[] }>(
      `/api/skills/${encodeURIComponent(slug)}/file-history?path=${encodeURIComponent(path)}&limit=${limit}`,
    ),
  skillTags: (slug: string) =>
    api.get<{ items: TagItem[] }>(`/api/skills/${encodeURIComponent(slug)}/tags`),
  skillTagCreate: (slug: string, name: string, message = "") =>
    api.post<{ ok: boolean; name: string }>(
      `/api/skills/${encodeURIComponent(slug)}/tags`,
      { name, message },
    ),
  fileTree: (slug: string) =>
    api.get<{ tree: FileEntry[] }>(`/api/skills/${encodeURIComponent(slug)}/files`),
  // ---- P3 CRUD ----
  /** 新建 Skill（E-07）：`template` ∈ minimal | with-deps | with-scripts。 */
  skillCreate: (input: {
    slug: string;
    name: string;
    description: string;
    category?: string;
    template?: string;
  }) =>
    api.post<{
      slug: string;
      commit: string;
      hash: string;
      gate: GateFinding[];
      gate_has_error: boolean;
    }>("/api/skills", {
      slug: input.slug,
      name: input.name,
      description: input.description,
      category: input.category ?? "",
      template: input.template ?? "minimal",
    }),
  /** 删除预览/执行结果（FR-1.0.26 ② / 红线 16：runtime 磁盘不动）。 */
  skillDelete: (slug: string, dryRun: boolean, confirmRemovals: string[] = []) =>
    api.post<DeleteResult>(
      `/api/skills/${encodeURIComponent(slug)}/delete`,
      { dry_run: dryRun, confirm_removals: confirmRemovals },
    ),
  fileRead: (slug: string, path: string) =>
    api.get<FileContent>(
      `/api/skills/${encodeURIComponent(slug)}/files/content?path=${encodeURIComponent(path)}`,
    ),
  fileWrite: (slug: string, path: string, content: string, baseHash: string, message: string) =>
    api.post<WriteResult>(`/api/skills/${encodeURIComponent(slug)}/files`, {
      path,
      content,
      base_hash: baseHash,
      message,
    }),
  skillChecks: (slug: string) =>
    api.get<{
      has_error: boolean;
      findings: GateFinding[];
      decay: DecayMetrics | null;
      health: HealthSummary | null;
    }>(`/api/skills/${encodeURIComponent(slug)}/checks`),

  // ---- P3 CONFLICT 四选一 ----
  // ---- P7 沙盒（FR-2.4.3 / FR-3.10）----
  health: () => api.get<{ status: string; sandbox: boolean }>("/api/health"),
  /** 恢复预置演示数据（仅沙盒部署存在；daemon 离线 → 503）。
   *  E-06.4：长任务 → `202 {task_id}`，进度用 `sandboxResetStatus` 轮询。 */
  sandboxReset: () =>
    api.post<{ task_id: string; status: string; reused?: boolean }>("/api/sandbox/reset"),
  sandboxResetStatus: (taskId: string) =>
    api.get<{
      task_id: string;
      status: "running" | "succeeded" | "failed";
      result?: { restored: boolean; skills: string[]; seed_version?: string | null; verified?: boolean } | null;
      error?: string | null;
      code?: string | null;
    }>(`/api/sandbox/reset/${encodeURIComponent(taskId)}`),

  // ---- P5 日志（FR-1.1.12）----
  /** 拉取 daemon 本地日志（云端代理 → daemon 端 `logs` 任务）。daemon 离线 → 503。 */
  logsView: (params: {
    mode?: "daemon" | "api" | "mcp";
    tail?: number;
    level?: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
    op_id?: string;
  }) => {
    const q = new URLSearchParams();
    q.set("mode", params.mode ?? "daemon");
    q.set("tail", String(params.tail ?? 100));
    q.set("level", params.level ?? "DEBUG");
    if (params.op_id) q.set("op_id", params.op_id);
    return api.get<LogsResponse>(`/api/logs?${q.toString()}`);
  },
  resolveConflict: (
    slug: string,
    runtimeKey: string,
    decision: ConflictDecision,
    confirmedRemovals: string[] = [],
    author = "web <web@skillos.local>",
  ) =>
    api.post<ResolveConflictResult>(
      `/api/skills/${encodeURIComponent(slug)}/conflicts/${encodeURIComponent(runtimeKey)}/resolve`,
      {
        decision,
        confirmed_removals: confirmedRemovals,
        author,
      },
    ),

  // ---- P3 Settings 页（FR-2.4.10 / T26）----
  // Runtime 自动发现 / 已纳管列表 / custom 诊断 / 协议 / 守护。
  /** 未纳管 + 已纳管 Runtime 一次返回；前端按 managed 字段切两段。 */
  runtimesDiscovered: (force = false) =>
    api.get<DiscoveredResponse>(
      `/api/runtimes/discovered${force ? "?force=true" : ""}`,
    ),
  /** 创建 custom Runtime 空条目（FR-1.0.34 / T25）。key 必须 `custom:<name>`。 */
  runtimesCreateCustom: (body: {
    key: string;
    name?: string;
    root_path: string;
    scope?: string;
    discover_only?: boolean;
  }) => api.post<DiscoveredRuntime>("/api/runtimes/custom", body),
  /** 编辑 custom Runtime 路径；discover_only 显式传 = 改。 */
  runtimesUpdateCustom: (
    key: string,
    body: { root_path: string; discover_only?: boolean | null },
  ) => api.patch<DiscoveredRuntime>(`/api/runtimes/${encodeURIComponent(key)}`, body),
  /** 删除 custom Runtime（NFR-11.4 / T25），同时清理 binding。 */
  runtimesDeleteCustom: (key: string) =>
    api.delete<{ key: string; deleted: boolean }>(
      `/api/runtimes/${encodeURIComponent(key)}`,
    ),
  /** builtin 停用 / 启用（FR-2.4.10）：停用 = 矩阵隐藏 + 不分发 + 不扫描。 */
  runtimesSetEnabled: (key: string, enabled: boolean) =>
    api.post<{ key: string; enabled: boolean }>(
      `/api/runtimes/${encodeURIComponent(key)}/enabled`,
      { enabled },
    ),
  /** custom Runtime 路径诊断（FR-1.0.33 / D9）：存在性 + 命中数 + 错误透传。 */
  runtimesDiagnose: (key: string) =>
    api.get<{
      path: string;
      exists: boolean;
      skill_count: number;
      error: string | null;
      evidence: string;
    }>(`/api/runtimes/${encodeURIComponent(key)}/diagnose`),
  /** 协议版本状态（FR-1.0.29 / T18）：sticky + HEAD trailer + 一致性。daemon 离线 → 503。 */
  protocolStatus: () => api.get<ProtocolStatus>("/api/protocol/status"),
  /** 守护状态（FR-2.4.10 Settings 区块）：云端直读 hub，离线时仍可用。 */
  daemonStatus: () => api.get<DaemonStatusResponse>("/api/daemon/status"),
  /** 用量上报（N-01.4/9）：向在线 daemon 拉一次聚合行并合入云端。
   *  合并是覆盖式 ⇒ 重复调用幂等；设备侧开关关闭时抛 `REPORT_DISABLED`。 */
  usageSync: () =>
    api.post<{ merged: number; device_id: string }>("/api/daemon/usage/sync"),
  /** 跨设备按 skill 汇总（N-01.6：只见总量，无个人明细）。 */
  usageSummary: (days = 30) =>
    api.get<UsageSummaryResponse>(
      `/api/daemon/usage/summary?days=${encodeURIComponent(String(days))}`,
    ),
  /** 管理者视图（N-01.7/8）：Top / 零调用 / 僵尸（口径 = 实测 + 折旧，见 zombie_basis）。 */
  managerUsage: (days = 30) =>
    api.get<ManagerUsageResponse>(
      `/api/usage/summary?days=${encodeURIComponent(String(days))}`,
    ),
  /** 单 skill 调用记录 + session 调用链（N-11.7）。事件 30 天滚动清理。 */
  skillUsage: (slug: string, limit = 100) =>
    api.get<SkillUsageResponse>(
      `/api/skills/${encodeURIComponent(slug)}/usage?limit=${limit}`,
    ),
  /** 设备侧开关（V2-T1）：采集 / 上报 / L2 会话路径。 */
  settings: () => api.get<SettingsResponse>("/api/settings"),
  saveSettings: (items: Record<string, unknown>) =>
    api.put<SettingsResponse>("/api/settings", { items }),
  /** 体检报告（N-02）：本地表，daemon 生成。 */
  reports: (limit = 20) =>
    api.get<{ items: HealthReportSummary[] }>(`/api/reports?limit=${limit}`),
  report: (id: string) =>
    api.get<HealthReportDetail>(`/api/reports/${encodeURIComponent(id)}`),

  // ---- 规则面板 / 相似对（M3：N-03 / N-08 / E-04 / N-07）----
  /** 规则面板：生效规则 + 无效规则（含原因）+ ignore 白名单 + ruleset_hash。 */
  rules: () => api.get<RulesPanel>("/api/rules"),
  /** 相似对（≥75% 提示，≥90% 带 suggestion）+ 已弃用清单。 */
  similarity: () =>
    api.get<{ items: SimilarityItem[]; deprecated: DeprecatedSkill[] }>("/api/similarity"),
  /** 回收落选方（N-07.3）：`keep` 决定谁是 drop；真跑必须原样回传 removals。 */
  similarityResolve: (
    a: string, b: string, keep: string,
    opts: { dryRun: boolean; confirmRemovals?: string[] },
  ) => api.post<ResolveResult>(
    `/api/similarity/${encodeURIComponent(a)}/${encodeURIComponent(b)}/resolve`,
    {
      keep,
      dry_run: opts.dryRun,
      confirm_removals: opts.confirmRemovals ?? [],
    },
  ),
  /** 立即比对（TQ-03）：覆盖式落库，重复点不新增行。 */
  similarityRun: () => api.post<SimilarityRunResult>("/api/similarity/run"),
  /** 上游「立即检查」（E-08.1）：daemon 侧跑 ls-remote，云端只转发。 */
  upstreamCheck: (slug: string) =>
    api.post<{ slug: string; status: string; detail?: string | null }>(
      `/api/skills/${encodeURIComponent(slug)}/upstream/check`,
    ),

  // ---- P3 Settings 页 · Token 区块（FR-2.4.10）----
  /** 设备列表（已配对 daemon / 撤销时间）。 */
  listDevices: () => api.get<DeviceItem[]>("/api/devices"),
  /** 撤销设备（FR-1.2.8）：置 revoked_at + 立即断连该 daemon。 */
  revokeDevice: (tokenId: string) =>
    api.delete<{ id: string; revoked: boolean }>(
      `/api/devices/${encodeURIComponent(tokenId)}`,
    ),
  /** 签发配对码（6 位一次性，10 分钟有效）。 */
  issuePairingCode: () =>
    api.post<{ code: string; ttl_s: number }>("/api/pairing/code", {}),
};

// ---- P3 Settings 页（FR-2.4.10 / T26）类型 ----

/** Runtime 条目（discovered / managed / candidates 三段共用）。 */
export interface DiscoveredRuntime {
  key: string;
  name: string;
  path: string;
  custom: boolean;
  managed: boolean;
  discover_only: boolean;
  path_exists?: boolean;
  skill_count?: number;
  version?: string;
  enabled?: boolean;
  scope?: string;
  created_at?: string | null;
  evidence?: string;
  verified_at?: string | null;
  /** 命中 = 该路径下至少一个 SKILL.md（与 discover 服务端命中判据同语义） */
  matched?: boolean;
  /** builtin 已停用（FR-2.4.10）：显示在未纳管段，带"启用"按钮 */
  disabled?: boolean;
}

export interface DiscoveredResponse {
  scanned_at: string | null;
  home: string | null;
  managed: DiscoveredRuntime[];
  candidates: DiscoveredRuntime[];
}

export interface ProtocolStatus {
  /** 本机支持的最大协议版本. daemon 离线时 = null. */
  current_supported: number | null;
  sticky_version: number | null;
  trailer_version: number | null;
  head_message_first_line: string | null;
  /** 三态合并: 协议一致 + daemon 可用. ok=false 时看 reason. */
  ok: boolean;
  read_only: boolean;
  reason: string | null;
}

export interface DaemonAgent {
  agent_id: string;
  token_id: string;
  connected_at: number;
  uptime_s: number;
}

export interface DaemonStatusResponse {
  online: boolean;
  agents: DaemonAgent[];
  agent_count: number;
}

/** 云端用量汇总的一行（N-01.6：只有聚合值，无个人明细）。 */
export interface UsageSummaryItem {
  skill_id: string;
  count: number;
  tokens_in: number;
  tokens_out: number;
  device_count: number;
}

export interface UsageSummaryResponse {
  days: number;
  items: UsageSummaryItem[];
}

// ---------------------------------------------------------------- 管理者视图（N-01.7/8）

export interface ManagerUsageTopItem {
  skill_id: string;
  count: number;
  tokens_in: number;
  tokens_out: number;
}

export interface ManagerUsageResponse {
  days: number;
  total_skills: number;
  top: ManagerUsageTopItem[];
  zero_called: string[];
  /** 僵尸 = 90 天零调用 ∩ staleness=degraded（口径单点在服务端） */
  zombie: string[];
  zombie_basis: string;
}

// ---------------------------------------------------------------- 调用记录（N-11.7）

export interface SkillUsageEvent {
  at: string;
  op: string;
  via: string;
  runtime: string;
  session_id: string;
  duration_ms: number | null;
  ok: boolean;
  tokens_in: number;
  tokens_out: number;
}

export interface SkillUsageStep {
  at: string;
  slug: string;
  op: string;
  ok: boolean;
  duration_ms: number | null;
}

export interface SkillUsageResponse {
  slug: string;
  events: SkillUsageEvent[];
  sessions: { session_id: string; steps: SkillUsageStep[] }[];
}

// ---------------------------------------------------------------- 设备侧开关（V2-T1）

export interface SettingsResponse {
  items: Record<string, unknown>;
}

// ---------------------------------------------------------------- 体检报告（N-02）

export interface HealthReportSummary {
  id: string;
  period_start: string | null;
  period_end: string | null;
  created_at: string | null;
  unchanged: boolean;
  new_drift: number;
  new_gate_errors: number;
  newly_degraded: number;
  avg_health: number | null;
}

export interface HealthReportDetail {
  id: string;
  unchanged: boolean;
  period_start: string | null;
  period_end: string | null;
  created_at: string | null;
  period: { start: string | null; end: string | null };
  drift: {
    counts: Record<string, { now: number; prev: number; delta: number }>;
    new_count: number;
    new: { slug: string; runtime_key: string; state: string }[];
    /** null = 正常统计；有值 = 本期无法统计（如空仓库），原因直出 */
    unavailable: string | null;
  };
  gate: {
    errors_now: number;
    new_count: number;
    new: { slug: string; rule: string }[];
  };
  decay: { degraded_now: number; count: number; newly_degraded: string[] };
  health: {
    avg: number | null;
    drops: { slug: string; from: number; to: number; delta: number }[];
  };
}

/** 配对设备（FR-2.4.10 Token 区块）。 */
export interface DeviceItem {
  id: string;
  agent_id: string;
  name: string | null;
  platform: string | null;
  last_seen_at: string | null;
  revoked_at: string | null;
}
