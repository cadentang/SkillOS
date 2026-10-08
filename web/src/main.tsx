import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // WS 事件驱动失效（04 §2.7.3），默认轮询只作兜底
      staleTime: 30_000,
      retry: 1,
      // 焦点不触发重取：drift 类查询的 refetch 会向 daemon 下发 status 任务
      // （全盘扫描），开发时 alt-tab 一来一回就是一次。事件驱动 + 显式
      // refetchInterval 兜底已足够（Dashboard 各查询上标注）。
      refetchOnWindowFocus: false,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
