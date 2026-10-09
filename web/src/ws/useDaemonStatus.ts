import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { tokenStore } from "../api/client";

/** FR-2.4.4：极简全局 daemon 状态（只存 connected / last_seen）。
 *  收到 daemon_status{connected:false} 立即切换，不等待下一轮询周期。
 *  写按钮的 disabled 都由它派生；WS 事件到达时 invalidateQuery（04 §2.7.3）。
 *
 *  事件：daemon_status → 连接态；drift_changed → 数据变更权威信号；
 *  task_done → 仅写集失效 ['drift'] / ['gate']（04 §4.2）。连接本身断开
 *  （云端不可达）也视为 daemon 状态未知 → 置 offline。 */
export interface DaemonStatus {
  connected: boolean;
  lastSeen: string | null;
}

/** 会改变 canonical / 磁盘状态的 op —— task_done 只有落在写集里才失效
 *  drift/gate。曾因无差别失效读任务（status/gate_summary/logs）重取自身
 *  又产生 task_done，形成请求自激风暴；读任务一律不失效。集合与云端
 *  WS_TASK_OPS 的写子集对齐（daemon 写后即时上报 drift_changed 的
 *  {push, rollback, pull, tag_create} + 不上报的其余写 op）。 */
const WRITE_OPS: ReadonlySet<string> = new Set([
  "push",
  "pull",
  "rollback",
  "file_write",
  "resolve_conflict",
  "skill_create",
  "skill_delete",
  "tag_create",
  "sandbox_reset",
]);

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
            op?: string;
          };
          if (msg.type === "daemon_status") {
            setStatus({
              connected: !!msg.connected,
              lastSeen: msg.connected ? new Date().toLocaleTimeString() : null,
            });
          } else if (msg.type === "drift_changed") {
            // 数据变更权威信号：无条件失效（04 §2.7.3）
            void queryClient.invalidateQueries({ queryKey: ["drift"] });
            void queryClient.invalidateQueries({ queryKey: ["gate"] });
          } else if (msg.type === "task_done" && msg.op && WRITE_OPS.has(msg.op)) {
            // 只有写任务完成才失效 —— 读任务（status/gate_summary/logs…）
            // 的重取自身也产生 task_done，无差别失效会自激成请求风暴
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
