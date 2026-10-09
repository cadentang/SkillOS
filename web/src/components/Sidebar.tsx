import { NavLink } from "react-router-dom";
import { useDaemonStatusCtx } from "../ws/DaemonStatusContext";

/**
 * Skill OS 控制台侧栏（设计稿 app/index.html §03 §04 §05 sidebar）。
 *
 *  236px / surface / 1px 右边框
 *  顶部：brand（30×30 蓝圆 logo + Skill OS）
 *  中部：三 nav（能力总览 / Skill 库 / 团队协作），active 态用 --primary-soft + --primary
 *  底部：daemon 状态卡（surface-alt + 1px border + 10px radius）
 *
 *  当前阶段不做 1440 缩放 / 主题切换器 / 6 页面 stage（A1 决策）；
 *  不动 React Router 数据层。
 */

interface NavItem {
  to: string;
  label: string;
  icon: React.ReactNode;
}

const NAV_ITEMS: NavItem[] = [
  {
    to: "/",
    label: "能力总览",
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
        <rect x="2" y="2" width="5" height="5" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
        <rect x="9" y="2" width="5" height="5" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
        <rect x="2" y="9" width="5" height="5" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
        <rect x="9" y="9" width="5" height="5" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
      </svg>
    ),
  },
  {
    to: "/skills",
    label: "Skill 库",
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
        <rect x="2.5" y="2.5" width="11" height="11" rx="3" stroke="currentColor" strokeWidth="1.5" />
        <path d="M5.5 8h5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    to: "/teams",
    label: "团队协作",
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
        <circle cx="6" cy="5.5" r="2.5" stroke="currentColor" strokeWidth="1.5" />
        <path d="M2 13c0-2.2 1.8-4 4-4s4 1.8 4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        <path d="M11.2 4.4a2.5 2.5 0 0 1 0 4.4M12.6 13c0-1.5-.5-2.9-1.5-3.8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    to: "/runtimes",
    label: "Runtime 设置",
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
        <rect x="2.5" y="2.5" width="11" height="11" rx="3" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="6" cy="6" r="0.8" fill="currentColor" />
        <circle cx="10" cy="6" r="0.8" fill="currentColor" />
        <circle cx="6" cy="10" r="0.8" fill="currentColor" />
        <circle cx="10" cy="10" r="0.8" fill="currentColor" />
      </svg>
    ),
  },
];

export default function Sidebar() {
  const daemon = useDaemonStatusCtx();
  return (
    <aside
      className="flex shrink-0"
      style={{
        width: "var(--sidebar-w)",
        background: "var(--surface)",
        borderRight: "1px solid var(--border)",
      }}
    >
      <div className="flex min-w-0 flex-1 flex-col gap-xs p-md">
        <div className="flex items-center gap-sm pb-sm">
          <div
            className="flex shrink-0 items-center justify-center rounded-[var(--radius-control)]"
            style={{ width: 30, height: 30, background: "var(--primary)" }}
            aria-hidden
          >
            <svg width="18" height="18" viewBox="0 0 16 16" fill="none">
              <path d="M8 1.5L14 4.8L8 8.1L2 4.8L8 1.5Z" fill="#fff" />
              <path
                d="M2 8L8 11.3L14 8"
                stroke="#fff"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
                opacity=".65"
              />
              <path
                d="M2 11.2L8 14.5L14 11.2"
                stroke="#fff"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
                opacity=".35"
              />
            </svg>
          </div>
          <span className="text-base font-bold tracking-tight">Skill OS</span>
        </div>

        <nav className="mt-xs flex flex-1 flex-col gap-xs pt-xs">
          {NAV_ITEMS.map((it) => (
            <NavLink
              key={it.to}
              to={it.to}
              end={it.to === "/"}
              className={({ isActive }) =>
                [
                  "flex h-9 items-center gap-sm whitespace-nowrap rounded-[var(--radius-control)] px-sm text-[13px] font-medium transition-colors",
                  isActive
                    ? "bg-[var(--primary-soft)] text-[var(--primary)]"
                    : "text-[var(--text-secondary)] hover:bg-[var(--surface-alt)] hover:text-[var(--text-primary)]",
                ].join(" ")
              }
            >
              {it.icon}
              <span>{it.label}</span>
            </NavLink>
          ))}
        </nav>

        <div
          className="mt-md flex items-center gap-sm px-sm py-sm"
          style={{
            background: "var(--surface-alt)",
            border: "1px solid var(--border)",
            borderRadius: 10,
            fontSize: "var(--font-caption)",
            fontWeight: 500,
          }}
        >
          <span
            className="inline-block h-2 w-2 shrink-0 rounded-full"
            style={{
              background: daemon.connected ? "var(--success)" : "var(--drift-missing)",
            }}
            aria-hidden
          />
          <span style={{ color: daemon.connected ? "var(--text-primary)" : "var(--text-secondary)" }}>
            {daemon.connected ? "daemon 在线" : "daemon 离线"}
          </span>
        </div>
      </div>
    </aside>
  );
}
