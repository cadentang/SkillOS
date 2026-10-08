import { Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import Sidebar from "./Sidebar";
import DaemonStatusBadge from "./DaemonStatusBadge";
import { DaemonStatusProvider } from "../ws/DaemonStatusContext";
import { endpoints } from "../api/endpoints";

/** Layout。沙盒模式（FR-2.4.3 / 04 §2.7.7）：顶部常驻「演示模式」横幅 ——
 *  沙盒是可写隔离（隔离靠文件系统边界，不靠 UI 禁用），横幅只做标识，
 *  scan / push / pull / 编辑 / 回滚全部可用。 */

export default function Layout() {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: endpoints.health,
    retry: false,
    staleTime: Infinity, // 沙盒标志部署期不变
  });
  const sandbox = health.data?.sandbox ?? false;

  return (
    <DaemonStatusProvider>
      <div className="flex min-h-screen">
        <Sidebar />
        <div className="flex-1">
          {sandbox && (
            <div
              className="px-md py-xs text-center text-[var(--font-caption)]"
              style={{ background: "var(--primary)", color: "var(--inverse)" }}
            >
              演示模式 · 数据在沙盒容器内，可随意修改；侧栏或总览页可一键重置
            </div>
          )}
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
    </DaemonStatusProvider>
  );
}
