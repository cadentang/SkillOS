import { NavLink } from "react-router-dom";

const items = [
  { to: "/", label: "能力总览" },
  { to: "/teams", label: "团队协作" },
];

export default function Sidebar() {
  return (
    <aside
      className="shrink-0 border-r"
      style={{ width: "var(--sidebar-w)", borderColor: "var(--border)" }}
    >
      <div className="px-md py-md">
        <span className="font-semibold">Skill OS</span>
        <span
          className="ml-xs rounded-[var(--radius-tag)] px-xs text-[var(--font-caption)]"
          style={{ background: "var(--primary-soft)", color: "var(--primary)" }}
        >
          控制台
        </span>
      </div>
      <nav className="flex flex-col gap-xs p-sm">
        {items.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) =>
              `rounded-[var(--radius-control)] px-md py-sm ${
                isActive
                  ? "bg-[var(--primary-soft)] text-[var(--primary)]"
                  : "text-[var(--text-secondary)]"
              }`
            }
          >
            {item.label}
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
