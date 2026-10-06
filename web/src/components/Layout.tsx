import { Outlet } from "react-router-dom";
import Sidebar from "./Sidebar";
import DaemonStatusBadge from "./DaemonStatusBadge";

export default function Layout() {
  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex-1">
        <header
          className="flex items-center justify-end border-b px-md"
          style={{ height: "var(--topbar-h)", borderColor: "var(--border)" }}
        >
          <DaemonStatusBadge />
        </header>
        <main className="p-lg">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
