import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// 05 P1-7：全链路统一 127.0.0.1 替代 localhost
const API_TARGET = "http://127.0.0.1:9080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // 发行红线：编译/发行形态下前端不可见 —— 构建产物不产出 sourcemap。
  // （浏览器侧 JS 无法真正隐藏，关 sourcemap 是唯一可行的一刀；见 docs/10）
  build: { sourcemap: false },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true },
      "/ws": { target: API_TARGET, ws: true },
    },
  },
});
