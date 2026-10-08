import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { tokenStore } from "../api/client";

/** FR-2.4.4：极简全局 daemon 状态（只存 connected / last_seen）。
 *  收到 daemon_status{connected:false} 立即切换，不等待下一轮询周期。
 *  写按钮的 disabled 都由它派生；WS 事件到达时 invalidateQuery（04 §2.7.3）。
 *
 *  事件：daemon_status → 连接态；drift_changed / task_done → 失效 ['drift'] 查询。
 *  连接本身断开（云端不可达）也视为 daemon 状态未知 → 置 offline。 */
export interface DaemonStatus {
  connected: boolean;
  lastSeen: string | null;
}

export function useDaemonStatus(): DaemonStatus {
  const [status, setStatus] = useState<DaemonStatus>({
    connected: false,
    lastSeen: null,
  });
  const queryClient = useQueryClient();

  useEffect(() => {
    const token = tokenStore.get();
    if (!token) return;

    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${window.location.host}/ws/ui?token=${encodeURIComponent(token)}`;
    let ws: WebSocket | null = null;
    let closed = false;
    let retry: number | undefined;

    const connect = () => {
      ws = new WebSocket(url);
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data as string) as {
            type: string;
            connected?: boolean;
          };
          if (msg.type === "daemon_status") {
            setStatus({
              connected: !!msg.connected,
              lastSeen: msg.connected ? new Date().toLocaleTimeString() : null,
            });
          } else if (msg.type === "drift_changed" || msg.type === "task_done") {
            // 事件驱动失效：不改本地平行状态，只让查询重取（04 §2.7.3）
            void queryClient.invalidateQueries({ queryKey: ["drift"] });
            void queryClient.invalidateQueries({ queryKey: ["gate"] });
          }
        } catch {
          // 非 JSON 帧忽略
        }
      };
      ws.onclose = () => {
        setStatus((s) => ({ ...s, connected: false }));
        if (!closed) retry = window.setTimeout(connect, 2000); // 指数可省：UI 通道 2s 够
      };
    };
    connect();

    return () => {
      closed = true;
      if (retry) window.clearTimeout(retry);
      ws?.close();
    };
  }, [queryClient]);

  return status;
}
