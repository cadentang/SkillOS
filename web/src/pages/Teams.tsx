/** P6 协作页（FR-2.3.x / FR-3.7 / FR-3.17）。
 *  单页含三视图：团队列表 → 选中后切到详情（成员 / Change Request / 管理者视图）。
 *  「不新增页面 / 不新增 tab」（P3 硬约束）：所有信息均在 /teams 路由内分类视图展示。 */

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "../api/endpoints";
import { ApiError } from "../api/client";
import type { TeamMember } from "../api/endpoints";

function errMsg(e: unknown): string {
  if (e instanceof Error) return e.message;
  if (e instanceof ApiError) return e.message;
  return String(e);
}

type View = "members" | "crs" | "dashboard";

export default function Teams() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState<View>("crs");
  const [newTeamName, setNewTeamName] = useState("");

  const teams = useQuery({
    queryKey: ["teams"],
    queryFn: () => endpoints.listTeams(),
  });
  const detail = useQuery({
    queryKey: ["team", selected],
    queryFn: () => endpoints.teamDetail(selected!),
    enabled: !!selected,
  });

  const dashboard = useQuery({
    queryKey: ["team-dashboard", selected],
    queryFn: () => endpoints.teamDashboard(selected!),
    enabled: !!selected && view === "dashboard",
  });
  const crs = useQuery({
    queryKey: ["team-crs", selected],
    queryFn: () => endpoints.listCRs(selected!),
    enabled: !!selected && view === "crs",
  });

  const myRole = detail.data?.role;
  const isAdmin = myRole === "owner";
  const isWriter = myRole === "owner" || myRole === "member";

  async function createTeam() {
    if (!newTeamName.trim()) return;
    await endpoints.createTeam(newTeamName.trim());
    setNewTeamName("");
    void queryClient.invalidateQueries({ queryKey: ["teams"] });
  }

  return (
    <div className="flex flex-col gap-md">
      <h1 className="font-semibold" style={{ fontSize: "var(--font-h1)" }}>
        团队协作
      </h1>

      <div className="flex gap-md">
        {/* 左侧：团队列表 + 新建 */}
        <aside
          className="w-[260px] shrink-0 rounded-[var(--radius-card)] border p-md"
          style={{ background: "var(--surface)", borderColor: "var(--border)" }}
        >
          <p className="font-medium">我所在的团队</p>
          <ul className="mt-sm flex flex-col gap-xs">
            {(teams.data?.items ?? []).map((t) => (
              <li key={t.id}>
                <button
                  onClick={() => {
                    setSelected(t.id);
                    setView("crs");
                  }}
                  className="w-full truncate rounded-[var(--radius-tag)] px-sm py-xs text-left"
                  style={{
                    background: selected === t.id ? "var(--primary-soft)" : "var(--surface-alt)",
                    color: selected === t.id ? "var(--primary)" : "var(--text-primary)",
                  }}
                >
                  <span className="font-medium">{t.name}</span>
                  <span
                    className="ml-xs font-mono text-[var(--font-caption)]"
                    style={{ color: "var(--text-secondary)" }}
                  >
                    {role_label(t.role)}
                  </span>
                </button>
              </li>
            ))}
            {(teams.data?.items ?? []).length === 0 && (
              <li className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
                暂无团队
              </li>
            )}
          </ul>
          <div className="mt-md flex flex-col gap-xs">
            <input
              value={newTeamName}
              onChange={(e) => setNewTeamName(e.target.value)}
              placeholder="新建团队名"
              className="rounded-[var(--radius-control)] border px-sm py-xs"
              style={{ borderColor: "var(--border)", background: "var(--surface-alt)" }}
            />
            <button
              onClick={createTeam}
              className="rounded-[var(--radius-control)] px-sm py-xs font-medium text-[var(--inverse)]"
              style={{ background: "var(--primary)" }}
            >
              创建团队（自动为 owner）
            </button>
          </div>
        </aside>

        {/* 右侧：选中团队的视图 */}
        <main className="min-w-0 flex-1">
          {!selected && (
            <p className="py-lg" style={{ color: "var(--text-secondary)" }}>
              选择左侧团队查看详情；或新建团队开始。
            </p>
          )}
          {selected && detail.isError && (
            <p style={{ color: "var(--danger)" }}>团队不可访问（{(detail.error as Error).message}）</p>
          )}
          {selected && detail.data && (
            <div className="flex flex-col gap-md">
              <div className="flex items-center justify-between">
                <div>
                  <h2 className="font-semibold" style={{ fontSize: "var(--font-h2)" }}>
                    {detail.data.name}
                  </h2>
                  <p
                    className="mt-xs font-mono text-[var(--font-caption)]"
                    style={{ color: "var(--text-secondary)" }}
                  >
                    你的角色：{role_label(myRole!)} · owner_id={detail.data.owner_id}
                  </p>
                </div>
                <div className="flex gap-sm">
                  <TabBtn on={view === "crs"} onClick={() => setView("crs")}>Change Request</TabBtn>
                  <TabBtn on={view === "members"} onClick={() => setView("members")}>成员</TabBtn>
                  <TabBtn on={view === "dashboard"} onClick={() => setView("dashboard")}>管理者视图</TabBtn>
                </div>
              </div>

              {view === "members" && (
                <MembersPanel
                  teamId={selected}
                  members={detail.data.members as TeamMember[]}
                  isAdmin={isAdmin}
                  onChanged={() => void queryClient.invalidateQueries({ queryKey: ["team", selected] })}
                />
              )}
              {view === "crs" && (
                <CRsPanel
                  teamId={selected}
                  crs={(crs.data?.items ?? detail.data.crs) ?? []}
                  isWriter={isWriter}
                  isAdmin={isAdmin}
                  onChanged={() => {
                    void queryClient.invalidateQueries({ queryKey: ["team", selected] });
                      void queryClient.invalidateQueries({ queryKey: ["team-crs", selected] });
                    }}
                />
              )}
              {view === "dashboard" && <DashboardPanel data={dashboard.data ?? null} loading={dashboard.isLoading} />}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

function role_label(r: "owner" | "member" | "viewer"): string {
  return r === "owner" ? "owner" : r === "member" ? "member" : "viewer";
}

function TabBtn({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className="rounded-[var(--radius-control)] px-md py-sm text-[var(--font-caption)]"
      style={{
        background: on ? "var(--primary-soft)" : "var(--surface)",
        color: on ? "var(--primary)" : "var(--text-secondary)",
        border: `1px solid ${on ? "var(--primary-border)" : "var(--border)"}`,
      }}
    >
      {children}
    </button>
  );
}

// ============================================================ 成员视图

function MembersPanel({
  teamId, members, isAdmin, onChanged,
}: {
  teamId: string;
  members: TeamMember[];
  isAdmin: boolean;
  onChanged: () => void;
}) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"member" | "viewer" | "owner">("member");
  const [err, setErr] = useState<string | null>(null);

  async function invite() {
    setErr(null);
    try {
      await endpoints.addMember(teamId, email.trim(), role);
      setEmail("");
      onChanged();
    } catch (e) {
      setErr(`邀请失败：${errMsg(e)}`);
    }
  }
  async function remove(uid: string) {
    setErr(null);
    try {
      await endpoints.removeMember(teamId, uid);
      onChanged();
    } catch (e) {
      setErr(`移除失败：${errMsg(e)}`);
    }
  }
  async function changeRole(uid: string, r: "member" | "viewer" | "owner") {
    setErr(null);
    try {
      await endpoints.changeRole(teamId, uid, r);
      onChanged();
    } catch (e) {
      setErr(`改权限失败：${errMsg(e)}`);
    }
  }

  return (
    <div
      className="rounded-[var(--radius-card)] border p-md"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      {err && <p className="mb-sm text-[var(--font-caption)]" style={{ color: "var(--danger)" }}>{err}</p>}
      <ul className="flex flex-col gap-xs">
        {members.map((m) => (
          <li
            key={m.user_id}
            className="flex items-center justify-between rounded-[var(--radius-tag)] px-sm py-xs"
            style={{ background: "var(--surface-alt)" }}
          >
            <div>
              <span className="font-medium">{m.name ?? m.email ?? m.user_id}</span>
              <span
                className="ml-sm font-mono text-[var(--font-caption)]"
                style={{ color: "var(--text-secondary)" }}
              >
                {m.role}
              </span>
            </div>
            {isAdmin && (
              <div className="flex gap-sm">
                <select
                  value={m.role}
                  onChange={(e) => void changeRole(m.user_id, e.target.value as "member" | "viewer" | "owner")}
                  className="rounded-[var(--radius-control)] border px-sm py-xs"
                  style={{ borderColor: "var(--border)" }}
                >
                  <option value="viewer">viewer</option>
                  <option value="member">member</option>
                  <option value="owner">owner</option>
                </select>
                <button
                  onClick={() => void remove(m.user_id)}
                  className="rounded-[var(--radius-control)] px-sm py-xs text-[var(--font-caption)]"
                  style={{ color: "var(--danger)", border: "1px solid var(--border)" }}
                >
                  移除
                </button>
              </div>
            )}
          </li>
        ))}
        {members.length === 0 && (
          <li className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
            暂无成员
          </li>
        )}
      </ul>

      {isAdmin && (
        <div className="mt-md flex items-center gap-sm">
          <input
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="用户 email"
            className="flex-1 rounded-[var(--radius-control)] border px-sm py-xs"
            style={{ borderColor: "var(--border)", background: "var(--surface-alt)" }}
          />
          <select
            value={role}
            onChange={(e) => setRole(e.target.value as "member" | "viewer" | "owner")}
            className="rounded-[var(--radius-control)] border px-sm py-xs"
            style={{ borderColor: "var(--border)" }}
          >
            <option value="viewer">viewer</option>
            <option value="member">member</option>
            <option value="owner">owner</option>
          </select>
          <button
            onClick={invite}
            className="rounded-[var(--radius-control)] px-sm py-xs font-medium text-[var(--inverse)]"
            style={{ background: "var(--primary)" }}
          >
            邀请
          </button>
        </div>
      )}
    </div>
  );
}

// ============================================================ CR 视图

function CRsPanel({
  teamId, crs, isWriter, isAdmin, onChanged,
}: {
  teamId: string;
  crs: import("../api/endpoints").TeamCR[];
  isWriter: boolean;
  isAdmin: boolean;
  onChanged: () => void;
}) {
  const [title, setTitle] = useState("");
  const [slug, setSlug] = useState("");
  const [body, setBody] = useState("");
  const [target, setTarget] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [openCrId, setOpenCrId] = useState<string | null>(null);

  async function create() {
    setErr(null);
    try {
      await endpoints.createCR(teamId, slug.trim(), title.trim(), body || undefined, undefined, target || undefined);
      setTitle(""); setSlug(""); setBody(""); setTarget("");
      onChanged();
    } catch (e) {
      setErr(`提交失败：${errMsg(e)}`);
    }
  }
  async function review(crId: string, action: "merged" | "closed") {
    setErr(null);
    try {
      await endpoints.reviewCR(teamId, crId, action);
      onChanged();
    } catch (e) {
      setErr(`审查失败：${errMsg(e)}`);
    }
  }

  return (
    <div className="flex flex-col gap-md">
      {err && <p className="text-[var(--font-caption)]" style={{ color: "var(--danger)" }}>{err}</p>}
      <ul className="flex flex-col gap-sm">
        {crs.map((c) => (
          <CRRow
            key={c.id} cr={c} isAdmin={isAdmin} isWriter={isWriter}
            open={openCrId === c.id} onToggle={() => setOpenCrId(openCrId === c.id ? null : c.id)}
            onReview={(action) => void review(c.id, action)}
          />
        ))}
        {crs.length === 0 && (
          <li className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
            暂无 Change Request
          </li>
        )}
      </ul>

      {isWriter && (
        <div
          className="rounded-[var(--radius-card)] border p-md"
          style={{ background: "var(--surface)", borderColor: "var(--border)" }}
        >
          <p className="font-medium">提交新 CR</p>
          <div className="mt-sm flex flex-col gap-sm">
            <input
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder="目标 skill slug"
              className="rounded-[var(--radius-control)] border px-sm py-xs font-mono text-[var(--font-mono)]"
              style={{ borderColor: "var(--border)", background: "var(--surface-alt)" }}
            />
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="标题"
              className="rounded-[var(--radius-control)] border px-sm py-xs"
              style={{ borderColor: "var(--border)", background: "var(--surface-alt)" }}
            />
            <input
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              placeholder="目标 commit（可选）"
              className="rounded-[var(--radius-control)] border px-sm py-xs font-mono text-[var(--font-mono)]"
              style={{ borderColor: "var(--border)", background: "var(--surface-alt)" }}
            />
            <textarea
              value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder="说明"
              className="rounded-[var(--radius-control)] border px-sm py-xs"
              style={{ borderColor: "var(--border)", background: "var(--surface-alt)", minHeight: 80 }}
            />
            <button
              onClick={create}
              disabled={!title.trim() || !slug.trim()}
              className="self-start rounded-[var(--radius-control)] px-md py-sm font-medium text-[var(--inverse)] disabled:opacity-40"
              style={{ background: "var(--primary)" }}
            >
              提交
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function CRRow({
  cr, isAdmin, isWriter, open, onToggle, onReview,
}: {
  cr: import("../api/endpoints").TeamCR;
  isAdmin: boolean;
  isWriter: boolean;
  open: boolean;
  onToggle: () => void;
  onReview: (action: "merged" | "closed") => void;
}) {
  const queryClient = useQueryClient();
  const crs = useQuery({
    queryKey: ["cr-detail", cr.id],
    queryFn: async () => {
      const teamId = cr.team_id;
      const data = await endpoints.getCR(teamId, cr.id);
      return { teamId, data };
    },
    enabled: open,
  });
  const [comment, setComment] = useState("");

  const status_color =
    cr.status === "merged" ? "var(--success)" :
    cr.status === "closed" ? "var(--text-secondary)" :
    "var(--warning)";

  async function postComment() {
    if (!comment.trim()) return;
    await endpoints.addCRComment(cr.team_id, cr.id, comment.trim());
    setComment("");
    void queryClient.invalidateQueries({ queryKey: ["cr-detail", cr.id] });
  }

  return (
    <li
      className="rounded-[var(--radius-card)] border p-md"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <div className="flex items-center justify-between">
        <div>
          <p className="font-medium">
            {cr.title}
            <span
              className="ml-sm rounded-[var(--radius-tag)] px-xs py-xs font-mono text-[var(--font-caption)]"
              style={{ background: "var(--surface-alt)", color: status_color }}
            >
              {cr.status}
            </span>
          </p>
          <p
            className="mt-xs font-mono text-[var(--font-caption)]"
            style={{ color: "var(--text-secondary)" }}
          >
            skill={cr.skill_id.slice(0, 10)} ·{" "}
            target={cr.target_commit ? cr.target_commit.slice(0, 10) : "—"} ·{" "}
            评论 {cr.comment_count}
          </p>
        </div>
        <button
          onClick={onToggle}
          className="rounded-[var(--radius-control)] px-sm py-xs text-[var(--font-caption)]"
          style={{ border: "1px solid var(--border)" }}
        >
          {open ? "收起" : "查看"}
        </button>
      </div>

      {open && (
        <div className="mt-md flex flex-col gap-sm">
          {crs.data?.data.body && (
            <p className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
              {crs.data.data.body}
            </p>
          )}
          <ul className="flex flex-col gap-xs">
            {(crs.data?.data.comments ?? []).map((c) => (
              <li
                key={c.id}
                className="rounded-[var(--radius-tag)] px-sm py-xs"
                style={{ background: "var(--surface-alt)" }}
              >
                <span className="font-mono text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
                  {c.author_id?.slice(0, 10)} · {c.at?.slice(0, 16).replace("T", " ")}
                </span>
                <p className="mt-xs whitespace-pre-wrap">{c.body}</p>
              </li>
            ))}
          </ul>
          {isWriter && cr.status === "open" && (
            <div className="flex gap-sm">
              <input
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                placeholder="评论"
                className="flex-1 rounded-[var(--radius-control)] border px-sm py-xs"
                style={{ borderColor: "var(--border)", background: "var(--surface-alt)" }}
              />
              <button
                onClick={postComment}
                className="rounded-[var(--radius-control)] px-sm py-xs"
                style={{ background: "var(--primary-soft)", color: "var(--primary)" }}
              >
                发送
              </button>
            </div>
          )}
          {isAdmin && cr.status === "open" && (
            <div className="flex gap-sm">
              <button
                onClick={() => onReview("merged")}
                className="rounded-[var(--radius-control)] px-md py-sm font-medium text-[var(--inverse)]"
                style={{ background: "var(--success)" }}
              >
                合入（merged）
              </button>
              <button
                onClick={() => onReview("closed")}
                className="rounded-[var(--radius-control)] px-md py-sm font-medium text-[var(--inverse)]"
                style={{ background: "var(--danger)" }}
              >
                驳回（closed）
              </button>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

// ============================================================ 管理者视图

function DashboardPanel({
  data, loading,
}: {
  data: import("../api/endpoints").TeamDashboard | null;
  loading: boolean;
}) {
  if (loading) return <p>加载中…</p>;
  if (!data) return <p style={{ color: "var(--text-secondary)" }}>无数据</p>;
  return (
    <div
      className="rounded-[var(--radius-card)] border p-md"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <div className="grid grid-cols-3 gap-md">
        <Stat title="覆盖度" value={`${data.coverage_pct}%`} sub={`${data.synced_cells}/${data.total_cells} 格子 SYNCED`} />
        <Stat title="僵尸占比" value={`${data.zombie_pct}%`} sub={`${data.total_skills} 个 team skill 中未纳管/孤儿的比例`} />
        <Stat title="健康度分布" value="" sub={
          <ul>
            {Object.entries(data.health).map(([k, v]) => (
              <li key={k}>{k}: {v}</li>
            ))}
          </ul>
        } />
      </div>
      <div className="mt-md">
        <p className="font-medium">成员贡献</p>
        <ul className="mt-sm flex flex-col gap-xs">
          {data.contribution.map((c) => (
            <li
              key={c.user_id ?? "unowned"}
              className="flex justify-between rounded-[var(--radius-tag)] px-sm py-xs"
              style={{ background: "var(--surface-alt)" }}
            >
              <span className="font-mono">{c.user_id ?? "（无主）"}</span>
              <span style={{ color: "var(--text-secondary)" }}>
                {c.skill_count} skill · {c.version_count} version
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function Stat({ title, value, sub }: { title: string; value: string; sub: React.ReactNode }) {
  return (
    <div
      className="rounded-[var(--radius-card)] border p-md"
      style={{ background: "var(--surface-alt)", borderColor: "var(--border)" }}
    >
      <p className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>{title}</p>
      <p className="mt-xs font-semibold" style={{ fontSize: "var(--font-h1)" }}>{value}</p>
      <p className="mt-xs text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>{sub}</p>
    </div>
  );
}