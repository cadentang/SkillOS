import { useDaemonStatusCtx } from "../ws/DaemonStatusContext";

/** FR-2.1.5 / FR-2.4.4：daemon 状态徽标。
 *  connected=false 时全局进入受限态：写操作禁用 + 遮罩，读操作保留但标注「数据可能过期」。
 *  状态派生集中在 DaemonStatusContext（WS daemon_status 事件），各页面不各判一次。 */
export default function DaemonStatusBadge() {
  const { connected, lastSeen } = useDaemonStatusCtx();
  return (
    <span
      className="inline-flex items-center gap-xs rounded-[var(--radius-full)] border px-sm"
      style={{ borderColor: "var(--border)" }}
    >
      <span
        className="inline-block h-2 w-2 rounded-full"
        style={{ background: connected ? "var(--drift-synced)" : "var(--drift-missing)" }}
      />
      <span className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
        {connected ? "daemon 在线" : `daemon 离线${lastSeen ? ` · ${lastSeen}` : ""}`}
      </span>
    </span>
  );
}
