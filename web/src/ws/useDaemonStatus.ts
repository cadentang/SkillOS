import { useEffect, useState } from "react";

/** FR-2.4.4：极简全局 daemon 状态（只存 connected / last_seen）。
 *  收到 daemon_status{connected:false} 立即切换，不等待下一轮询周期。
 *  写按钮的 disabled 都由它派生；P3 接入 /ws/ui 后替换占位实现。 */
export interface DaemonStatus {
  connected: boolean;
  lastSeen: string | null;
}

export function useDaemonStatus(): DaemonStatus {
  const [status, setStatus] = useState<DaemonStatus>({ connected: false, lastSeen: null });

  useEffect(() => {
    // TODO(P3)：连接 WS /ws/ui，订阅 daemon_status / drift_changed / task_* 事件，
    // 事件到达时 setStatus 并 invalidateQuery（04 §2.7.3：事件驱动失效，不改本地平行状态）。
    void setStatus;
  }, []);

  return status;
}
