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

export interface TeamCR {
  id: string;
  team_id: string;
  skill_id: string;
  title: string;
  body: string | null;
  status: "open" | "merged" | "closed";
  author_id: string | null;
  reviewer_id: string | null;
  base_version: string | null;
  target_commit: string | null;
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
  reviewCR: (teamId: string, crId: string, action: "merged" | "closed") =>
    api.post<TeamCR>(`/api/teams/${teamId}/change-requests/${crId}/review`, { action }),
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
  skillCreate: (slug: string, name: string, description: string) =>
    api.post<{
      slug: string;
      commit: string;
      hash: string;
      gate: GateFinding[];
      gate_has_error: boolean;
    }>("/api/skills", { slug, name, description }),
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
  /** 恢复预置演示数据（仅沙盒部署存在；daemon 离线 → 503）。 */
  sandboxReset: () => api.post<{ restored: boolean; skills: string[] }>("/api/sandbox/reset"),
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
};
