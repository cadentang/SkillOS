import { Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import Sidebar from "./Sidebar";
import DaemonStatusBadge from "./DaemonStatusBadge";
import { DaemonStatusProvider } from "../ws/DaemonStatusContext";
import { endpoints } from "../api/endpoints";

/** Layout（设计稿 app/index.html §03 §04 §05 shell）。
 *
 *  结构：
 *    <Sidebar 236> | <main>
 *                     ├ 沙盒横幅（演示模式，仅 P7 sandbox 部署存在）
 *                     ├ topbar（var(--topbar-h)=56，1px 底边）—— daemon 状态 + 业务页面 actions
 *                     ├ rule
 *                     └ content（p-lg=24，flex-1）—— Outlet
 *
 *  沙盒模式（FR-2.4.3 / 04 §2.7.7）：隔离靠文件系统边界，UI 全可用 —— 横幅只做标识。
 */

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
      <div className="flex min-h-screen" style={{ background: "var(--canvas)" }}>
        <Sidebar />
        <div className="flex min-w-0 flex-1 flex-col">
          {sandbox && (
            <div
              className="px-md py-xs text-center text-[var(--font-caption)]"
              style={{ background: "var(--primary)", color: "var(--inverse)" }}
            >
              演示模式 · 数据在沙盒容器内，可随意修改；侧栏或总览页可一键重置
            </div>
          )}
          <header
            className="flex shrink-0 items-center justify-between border-b px-lg"
            style={{ height: "var(--topbar-h)", borderColor: "var(--border)", background: "var(--surface)" }}
          >
            {/* 左：sandbox 横幅已挤过；详情页可在自己的 content 顶部做面包屑（设计稿 breadcrumbs） */}
            <div />
            <div className="flex items-center gap-sm">
              <DaemonStatusBadge />
            </div>
          </header>
          <main className="flex-1 overflow-hidden p-lg">
            <Outlet />
          </main>
        </div>
      </div>
    </DaemonStatusProvider>
  );
}
