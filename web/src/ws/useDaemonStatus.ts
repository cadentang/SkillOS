import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { tokenStore } from "../api/client";

/** FR-2.4.4：极简全局 daemon 状态（只存 connected / last_seen）。
 *  收到 daemon_status{connected:false} 立即切换，不等待下一轮询周期。
 *  写按钮的 disabled 都由它派生；WS 事件到达时 invalidateQuery（04 §2.7.3）。
 *
 *  事件：daemon_status → 连接态；drift_changed → 数据变更权威信号；
 *  usage_changed → 用量汇总重取（N-01.4）；
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
    // 见下方 daemon_status handler：只在「见过离线后重新上线」时失效一次
    let sawOffline = false;

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
            const conn = !!msg.connected;
            // daemon 重新上线（见过离线）→ 立即失效离线窗口期失败的读查询
            // （recent-sync / logs 的 30s / 5s 轮询等不起，否则错误态滞留
            // 到下个轮询周期，用户看到「记录读取失败」但徽标已绿）。
            // 安全性：daemon_status 不来自 daemon 任务，重取 logs 只产生一条
            // task_done(op=logs)，logs 不在 WRITE_OPS → 不再触发失效，
            // 无自激（区别于 task_done 无差别失效，见上方 WRITE_OPS 注释）。
            if (!conn) {
              sawOffline = true;
            } else if (sawOffline) {
              sawOffline = false;
              void queryClient.invalidateQueries({ queryKey: ["recent-sync"] });
              void queryClient.invalidateQueries({ queryKey: ["logs"] });
            }
            setStatus({
              connected: conn,
              lastSeen: conn ? new Date().toLocaleTimeString() : null,
            });
          } else if (msg.type === "drift_changed") {
            // 数据变更权威信号：无条件失效（04 §2.7.3）
            void queryClient.invalidateQueries({ queryKey: ["drift"] });
            void queryClient.invalidateQueries({ queryKey: ["gate"] });
          } else if (msg.type === "usage_changed") {
            // daemon 主动上报落库（N-01.4）：云端已合并 → 汇总视图要重取
            void queryClient.invalidateQueries({ queryKey: ["usage", "summary"] });
          } else if (msg.type === "cr.updated") {
            // E-05.4：CR 状态变更（含别人审的）→ 列表/详情/团队卡一起失效。
            // 不用 task_done 那套写集判断：CR 变更不走 daemon 任务，来自云端路由。
            void queryClient.invalidateQueries({ queryKey: ["team-crs"] });
            void queryClient.invalidateQueries({ queryKey: ["cr-detail"] });
            void queryClient.invalidateQueries({ queryKey: ["team"] });
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
