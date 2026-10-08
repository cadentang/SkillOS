import { createContext, useContext } from "react";
import { useDaemonStatus, type DaemonStatus } from "./useDaemonStatus";

/** daemon 连接态单例分发（FR-2.4.4）：WS 只连一次（Layout 挂载），
 *  Badge / 页面写按钮统一从 context 取，不各开连接。 */
const DaemonStatusContext = createContext<DaemonStatus>({
  connected: false,
  lastSeen: null,
});

export function DaemonStatusProvider({ children }: { children: React.ReactNode }) {
  const status = useDaemonStatus();
  return (
    <DaemonStatusContext.Provider value={status}>
      {children}
    </DaemonStatusContext.Provider>
  );
}

export function useDaemonStatusCtx(): DaemonStatus {
  return useContext(DaemonStatusContext);
}
