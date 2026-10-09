/** Runtime 设置页（FR-2.4.10 / T26 / FR-1.0.32~34 / T24~25）。
 *
 *  4 区块：Runtime（未纳管 + 已纳管 + custom 诊断）/ 守护 / 协议 / Token 配对。
 *  端点全部走 endpoints.*；写操作（加入 / 编辑 / 删除 custom）走 mutation。
 *  失败破坏性操作走原生 confirm / prompt 二确认（FR-1.0.26 守卫）。
 *
 *  后续可替换为 React modal：现在先用 native dialog 保 build 通过。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  endpoints,
  type DaemonStatusResponse,
  type DeviceItem,
  type DiscoveredResponse,
  type DiscoveredRuntime,
  type ProtocolStatus,
} from "../api/endpoints";
import { useDaemonStatusCtx } from "../ws/DaemonStatusContext";

export default function RuntimeSettings() {
  return (
    <div className="flex flex-col gap-md">
      <div>
        <h1 className="font-medium" style={{ fontSize: "var(--font-h1)" }}>
          Runtime 设置
        </h1>
        <p
          className="mt-xs text-[var(--font-caption)]"
          style={{ color: "var(--text-secondary)" }}
        >
          未纳管 Runtime 候选 + 已纳管 Runtime 列表 / 守护状态 / 协议版本 / Token 配对（FR-2.4.10 / T26）
        </p>
      </div>
      <div className="flex flex-col gap-md">
        <RuntimeBlock />
        <DaemonBlock />
        <ProtocolBlock />
        <TokenBlock />
      </div>
    </div>
  );
}

// ============================================================== Runtime 区块

function RuntimeBlock() {
  const queryClient = useQueryClient();
  const [diagInfo, setDiagInfo] = useState<{ key: string; data: any } | null>(null);

  const discovered = useQuery<DiscoveredResponse>({
    queryKey: ["runtimes", "discovered"],
    queryFn: () => endpoints.runtimesDiscovered(false),
    retry: false,
    refetchOnWindowFocus: false,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["runtimes"] });
  };

  const createMut = useMutation({
    mutationFn: (body: {
      key: string;
      name: string;
      root_path: string;
      discover_only: boolean;
    }) => endpoints.runtimesCreateCustom(body),
    onSuccess: invalidate,
  });

  const updateMut = useMutation({
    mutationFn: ({
      key,
      root_path,
      discover_only,
    }: {
      key: string;
      root_path: string;
      discover_only: boolean | null;
    }) => endpoints.runtimesUpdateCustom(key, { root_path, discover_only }),
    onSuccess: invalidate,
  });

  const deleteMut = useMutation({
    mutationFn: (key: string) => endpoints.runtimesDeleteCustom(key),
    onSuccess: invalidate,
  });

  const diagnoseMut = useMutation({
    mutationFn: (key: string) => endpoints.runtimesDiagnose(key),
    onSuccess: (data, key) => {
      setDiagInfo({ key, data });
    },
  });

  function joinRuntime(c: DiscoveredRuntime) {
    const displayName = window.prompt(
      `加入 Runtime 候选：${c.name}（${c.path}）\n\n请输入显示名（可空）`,
      c.name,
    );
    if (displayName === null) return;
    const keySlug = window.prompt(
      "请输入 custom 命名空间 key（小写字母 + 连字符）",
      c.key.toLowerCase().replace(/[^a-z0-9-]/g, "-"),
    );
    if (!keySlug) return;
    const key = `custom:${keySlug}`;
    createMut.mutate({
      key,
      name: displayName,
      root_path: c.path,
      discover_only: false,
    });
  }

  function editCustom(rt: DiscoveredRuntime) {
    const newPath = window.prompt(`编辑 custom 路径：${rt.name}`, rt.path);
    if (!newPath) return;
    updateMut.mutate({
      key: rt.key,
      root_path: newPath,
      discover_only: null,
    });
  }

  function deleteCustom(rt: DiscoveredRuntime) {
    if (
      !window.confirm(
        `确认删除 custom Runtime：${rt.name}（${rt.key}）？\n\n此操作会清理其所有 binding，且不可恢复。`,
      )
    )
      return;
    deleteMut.mutate(rt.key);
  }

  const data = discovered.data;
  const managed: DiscoveredRuntime[] = data?.managed ?? [];
  const candidates: DiscoveredRuntime[] = data?.candidates ?? [];
  const customList = managed.filter((r) => r.custom);

  return (
    <Section
      title="Runtime"
      subtitle={
        data
          ? `已纳管 ${managed.length} 个（builtin ${managed.filter((r) => !r.custom).length} + custom ${customList.length}） · 候选 ${candidates.length} 个`
          : "扫描中…"
      }
      actions={
        <button
          type="button"
          onClick={() => {
            void discovered.refetch();
          }}
          className="rounded-[var(--radius-control)] px-sm py-xs text-[var(--font-caption)]"
          style={{ background: "var(--surface-alt)", color: "var(--text-primary)" }}
        >
          重新扫描
        </button>
      }
    >
      {discovered.isError && (
        <ErrorBanner error={discovered.error} />
      )}

      <SubGroup title={`未纳管 Runtime 候选 (${candidates.length})`}>
        {candidates.length === 0 ? (
          <Empty>暂无候选（白名单命中 0 条）</Empty>
        ) : (
          candidates.map((c) => (
            <Row
              key={c.key}
              name={c.name}
              path={c.path}
              hint={
                c.path_exists
                  ? `路径命中 · ${c.skill_count ?? 0} 个 skill`
                  : "路径未命中"
              }
              hintColor={c.path_exists ? "ok" : "warn"}
              actions={
                <>
                  <Btn
                    label="加入"
                    onClick={() => joinRuntime(c)}
                    disabled={createMut.isPending}
                  />
                </>
              }
            />
          ))
        )}
      </SubGroup>

      <SubGroup title={`已纳管 Runtime (${managed.length})`}>
        {managed.length === 0 ? (
          <Empty>无</Empty>
        ) : (
          managed.map((rt) => (
            <Row
              key={rt.key}
              name={rt.name}
              path={rt.path}
              hint={
                rt.custom
                  ? rt.discover_only
                    ? "custom · discover_only"
                    : "custom · 已纳管"
                  : `builtin · v${rt.version ?? "?"}`
              }
              hintColor={rt.custom && rt.discover_only ? "warn" : "ok"}
              actions={
                <>
                  <Btn
                    label="诊断"
                    onClick={() => diagnoseMut.mutate(rt.key)}
                    disabled={diagnoseMut.isPending}
                  />
                  {rt.custom && (
                    <>
                      <Btn
                        label="编辑"
                        onClick={() => editCustom(rt)}
                        disabled={updateMut.isPending}
                      />
                      <Btn
                        label="删除"
                        onClick={() => deleteCustom(rt)}
                        danger
                        disabled={deleteMut.isPending}
                      />
                    </>
                  )}
                </>
              }
            />
          ))
        )}
      </SubGroup>

      {diagInfo && (
        <DiagPanel
          key={diagInfo.key}
          rtKey={diagInfo.key}
          data={diagInfo.data}
          onClose={() => setDiagInfo(null)}
        />
      )}
    </Section>
  );
}

function DiagPanel({
  rtKey,
  data,
  onClose,
}: {
  rtKey: string;
  data: { path: string; exists: boolean; skill_count: number; error: string | null };
  onClose: () => void;
}) {
  return (
    <div
      className="mt-sm flex flex-col gap-sm rounded-[var(--radius-card)] border p-sm"
      style={{ background: "var(--surface-alt)", borderColor: "var(--border)" }}
    >
      <div className="flex items-center justify-between">
        <span className="font-medium">诊断：{rtKey}</span>
        <button
          type="button"
          onClick={onClose}
          className="rounded-[var(--radius-control)] px-sm py-xs text-[var(--font-caption)]"
          style={{ color: "var(--text-secondary)" }}
        >
          ✕
        </button>
      </div>
      <KV k="path" v={data.path} />
      <KV k="exists" v={data.exists ? "是" : "否"} />
      <KV k="skill_count" v={String(data.skill_count)} />
      {data.error && <KV k="error" v={data.error} color="danger" />}
    </div>
  );
}

// ============================================================== 守护状态区块

function DaemonBlock() {
  const daemon = useDaemonStatusCtx();
  const status = useQuery<DaemonStatusResponse>({
    queryKey: ["daemon", "status"],
    queryFn: endpoints.daemonStatus,
    retry: false,
    refetchInterval: 10_000,
  });

  const data = status.data;
  return (
    <Section
      title="守护状态"
      subtitle={data ? (data.online ? `在线 · ${data.agent_count} 个 agent` : "离线") : "加载中…"}
    >
      {status.isError ? (
        <ErrorBanner error={status.error} />
      ) : data ? (
        <div className="flex flex-col gap-sm">
          <KV k="WS 在线" v={data.online ? "是" : "否"} color={data.online ? "ok" : "warn"} />
          {data.agents.length > 0 ? (
            data.agents.map((a) => (
              <Row
                key={a.agent_id}
                name={a.agent_id}
                path={a.token_id}
                hint={`uptime ${a.uptime_s}s`}
              />
            ))
          ) : (
            <Empty>无活跃 agent 连接</Empty>
          )}
          {daemon.lastSeen && (
            <p
              className="text-[var(--font-caption)]"
              style={{ color: "var(--text-secondary)" }}
            >
              WS 通道最后心跳：{daemon.lastSeen.toLocaleString()}
            </p>
          )}
        </div>
      ) : null}
    </Section>
  );
}

// ============================================================== 协议版本区块

function ProtocolBlock() {
  const prot = useQuery<ProtocolStatus>({
    queryKey: ["protocol", "status"],
    queryFn: endpoints.protocolStatus,
    retry: false,
  });

  const data = prot.data;
  return (
    <Section
      title="协议版本"
      subtitle={
        data
          ? data.ok
            ? data.read_only
              ? `只读模式（${data.reason ?? "?"}）`
              : "ok"
            : `异常（${data.reason ?? "?"}）`
          : "加载中…"
      }
    >
      {prot.isError ? (
        <ErrorBanner error={prot.error} />
      ) : data ? (
        <div className="flex flex-col gap-sm">
          <KV k="本机支持" v={String(data.current_supported)} />
          <KV k="sticky (protocol.json)" v={String(data.sticky_version ?? "—")} />
          <KV k="trailer (HEAD commit)" v={String(data.trailer_version ?? "—")} />
          {data.head_message_first_line && (
            <KV k="HEAD msg" v={data.head_message_first_line} />
          )}
          <KV
            k="一致性"
            v={data.ok ? "ok" : "mismatch"}
            color={data.ok ? "ok" : "warn"}
          />
        </div>
      ) : null}
    </Section>
  );
}

// ============================================================== Token 配对区块

function TokenBlock() {
  const queryClient = useQueryClient();
  const [pair, setPair] = useState<{ code: string; ttl_s: number } | null>(null);

  const devices = useQuery<DeviceItem[]>({
    queryKey: ["auth", "devices"],
    queryFn: endpoints.listDevices,
    retry: false,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["auth", "devices"] });
    void queryClient.invalidateQueries({ queryKey: ["daemon", "status"] });
  };

  const pairMut = useMutation({
    mutationFn: endpoints.issuePairingCode,
    onSuccess: (data) => setPair(data),
  });

  const revokeMut = useMutation({
    mutationFn: (id: string) => endpoints.revokeDevice(id),
    onSuccess: invalidate,
  });

  function revoke(d: DeviceItem) {
    if (
      !window.confirm(
        `确认撤销设备 ${d.name || d.agent_id}？\n\n此操作会立即断连该 daemon（FR-1.2.8）。`,
      )
    )
      return;
    revokeMut.mutate(d.id);
  }

  return (
    <Section
      title="Token 配对"
      subtitle={
        devices.data ? `已配对 ${devices.data.length} 个设备` : "加载中…"
      }
      actions={
        <button
          type="button"
          onClick={() => pairMut.mutate(undefined)}
          className="rounded-[var(--radius-control)] px-sm py-xs text-[var(--font-caption)]"
          style={{ background: "var(--primary-soft)", color: "var(--primary)" }}
          disabled={pairMut.isPending}
        >
          签发配对码
        </button>
      }
    >
      {pair && (
        <div
          className="mb-sm rounded-[var(--radius-card)] border p-sm"
          style={{ background: "var(--primary-soft)", borderColor: "var(--primary)" }}
        >
          <p className="font-mono text-2xl" style={{ color: "var(--primary)" }}>
            {pair.code}
          </p>
          <p
            className="mt-xs text-[var(--font-caption)]"
            style={{ color: "var(--text-secondary)" }}
          >
            10 分钟内有效（{pair.ttl_s}s） · daemon 用此码配对后会签发永久 token
          </p>
        </div>
      )}
      {devices.isError ? (
        <ErrorBanner error={devices.error} />
      ) : devices.data && devices.data.length > 0 ? (
        devices.data.map((d) => (
          <Row
            key={d.id}
            name={d.name || d.agent_id}
            path={d.platform || "unknown"}
            hint={
              d.revoked_at
                ? `已撤销 ${d.revoked_at}`
                : d.last_seen_at
                  ? `最后活跃 ${d.last_seen_at}`
                  : "从未活跃"
            }
            hintColor={d.revoked_at ? "warn" : "ok"}
            actions={
              !d.revoked_at && (
                <Btn
                  label="撤销"
                  onClick={() => revoke(d)}
                  danger
                  disabled={revokeMut.isPending}
                />
              )
            }
          />
        ))
      ) : (
        <Empty>暂无配对设备</Empty>
      )}
    </Section>
  );
}

// ============================================================== 通用 UI 部件

function Section({
  title,
  subtitle,
  actions,
  children,
}: {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section
      className="flex flex-col gap-sm rounded-[var(--radius-card)] border p-md"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <div className="flex items-start justify-between gap-sm">
        <div>
          <h2 className="font-medium" style={{ fontSize: "var(--font-h2)" }}>
            {title}
          </h2>
          {subtitle && (
            <p
              className="mt-xs text-[var(--font-caption)]"
              style={{ color: "var(--text-secondary)" }}
            >
              {subtitle}
            </p>
          )}
        </div>
        {actions && <div className="flex gap-sm">{actions}</div>}
      </div>
      <div className="flex flex-col gap-sm">{children}</div>
    </section>
  );
}

function SubGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-xs">
      <p
        className="text-[var(--font-caption)] font-medium"
        style={{ color: "var(--text-tertiary)" }}
      >
        {title}
      </p>
      <div className="flex flex-col gap-xs">{children}</div>
    </div>
  );
}

function Row({
  name,
  path,
  hint,
  hintColor,
  actions,
}: {
  name: string;
  path?: string;
  hint?: string;
  hintColor?: "ok" | "warn" | "danger";
  actions?: React.ReactNode;
}) {
  const hintStyle =
    hintColor === "ok"
      ? "var(--success)"
      : hintColor === "warn"
        ? "var(--warning)"
        : hintColor === "danger"
          ? "var(--danger)"
          : "var(--text-secondary)";
  return (
    <div
      className="flex items-center gap-sm rounded-[var(--radius-control)] border px-sm py-xs"
      style={{ background: "var(--surface-alt)", borderColor: "var(--border)" }}
    >
      <div className="min-w-0 flex-1">
        <p className="font-medium">{name}</p>
        {path && (
          <p
            className="truncate font-mono text-[var(--font-caption)]"
            style={{ color: "var(--text-secondary)" }}
            title={path}
          >
            {path}
          </p>
        )}
        {hint && (
          <p
            className="text-[var(--font-caption)]"
            style={{ color: hintStyle }}
          >
            {hint}
          </p>
        )}
      </div>
      {actions && <div className="flex shrink-0 gap-xs">{actions}</div>}
    </div>
  );
}

function Btn({
  label,
  onClick,
  danger,
  disabled,
}: {
  label: string;
  onClick: () => void;
  danger?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-[var(--radius-control)] px-sm py-xs text-[var(--font-caption)]"
      style={{
        background: danger ? "var(--danger-soft, transparent)" : "var(--primary-soft)",
        color: danger ? "var(--danger)" : "var(--primary)",
        opacity: disabled ? 0.5 : 1,
      }}
    >
      {label}
    </button>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p
      className="rounded-[var(--radius-control)] px-sm py-sm text-[var(--font-caption)]"
      style={{ color: "var(--text-secondary)", background: "var(--surface-alt)" }}
    >
      {children}
    </p>
  );
}

function ErrorBanner({ error }: { error: unknown }) {
  const msg =
    error instanceof Error ? error.message : String(error ?? "未知错误");
  return (
    <p
      className="rounded-[var(--radius-control)] px-sm py-sm text-[var(--font-caption)]"
      style={{ color: "var(--danger)", background: "var(--danger-soft, transparent)" }}
    >
      {msg}
    </p>
  );
}

function KV({ k, v, color }: { k: string; v: string; color?: "ok" | "warn" | "danger" }) {
  const vColor =
    color === "ok"
      ? "var(--success)"
      : color === "warn"
        ? "var(--warning)"
        : color === "danger"
          ? "var(--danger)"
          : "var(--text-primary)";
  return (
    <p
      className="flex gap-sm text-[var(--font-caption)]"
      style={{ color: "var(--text-secondary)" }}
    >
      <span className="shrink-0">{k}</span>
      <span className="font-mono" style={{ color: vColor }}>
        {v}
      </span>
    </p>
  );
}
