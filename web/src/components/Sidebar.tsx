import { NavLink } from "react-router-dom";

const items = [
  { to: "/", label: "Dashboard" },
  { to: "/skills", label: "Skills" },
  { to: "/teams", label: "Teams" },
];

export default function Sidebar() {
  return (
    <aside
      className="shrink-0 border-r"
      style={{ width: "var(--sidebar-w)", borderColor: "var(--border)" }}
    >
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
