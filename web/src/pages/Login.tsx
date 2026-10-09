import { FormEvent, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { endpoints } from "../api/endpoints";
import { tokenStore } from "../api/client";

/** 登录页（FR-2.4.1 / 设计稿 app/index.html §06）。
 *
 * 视觉：左 135deg 蓝渐变（#2663EB→#122980）+ logo + title + 3 项 features，
 *      右 560px 表单 + tip。
 *
 * 注册模式（demo 现场需要）：URL `?register=1` 启用，仍走 endpoints.register，
 * 默认页面 100% 对齐设计稿（无注册切换按钮）。
 */

function LoginLogo() {
  return (
    <svg width="30" height="30" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path d="M8 1.5L14 4.8L8 8.1L2 4.8L8 1.5Z" fill="#2663EB" />
      <path d="M2 8L8 11.3L14 8" stroke="#2663EB" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" opacity=".65" />
      <path d="M2 11.2L8 14.5L14 11.2" stroke="#2663EB" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" opacity=".35" />
    </svg>
  );
}

function FeatureCheck() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden>
      <circle cx="9" cy="9" r="8" fill="#fff" fillOpacity="0.16" />
      <path
        d="M5.5 9.2L7.8 11.5L12.5 6.8"
        stroke="#fff"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export default function Login() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [mode, setMode] = useState<"login" | "register">(
    params.get("register") === "1" ? "register" : "login",
  );
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // 跟随 ?register=1 / ?register=0 切换（默认隐藏入口，便于 demo 现场打开 register 模式）
  useEffect(() => {
    const r = params.get("register");
    if (r === "1") setMode("register");
    if (r === "0") setMode("login");
  }, [params]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mode === "register") {
        await endpoints.register(email, password);
      }
      const { token } = await endpoints.login(email, password);
      tokenStore.set(token);
      navigate("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen">
      {/* 左：渐变 + 标题 + features */}
      <div
        className="flex min-w-0 flex-1 flex-col items-center justify-center gap-lg"
        style={{
          padding: 72,
          background: "linear-gradient(135deg, #2663EB 0%, #122980 100%)",
        }}
      >
        <div
          className="flex items-center justify-center"
          style={{ width: 56, height: 56, borderRadius: 14, background: "#fff" }}
        >
          <LoginLogo />
        </div>
        <div
          className="text-center font-bold tracking-tight text-[var(--inverse)]"
          style={{ fontSize: 32, letterSpacing: "-.6px" }}
        >
          Skill OS
        </div>
        <div
          className="text-center"
          style={{ fontSize: "var(--font-h3)", color: "rgba(255,255,255,.78)" }}
        >
          Agent 能力的包管理器与分发层
        </div>

        <div className="mt-md flex w-[460px] flex-col gap-sm pt-md">
          <div className="flex items-center gap-sm">
            <FeatureCheck />
            <span style={{ color: "rgba(255,255,255,.86)" }}>扫描 4 个 Runtime 目录，一次纳管去重</span>
          </div>
          <div className="flex items-center gap-sm">
            <FeatureCheck />
            <span style={{ color: "rgba(255,255,255,.86)" }}>漂移 7 态可见，绝不谎报同步</span>
          </div>
          <div className="flex items-center gap-sm">
            <FeatureCheck />
            <span style={{ color: "rgba(255,255,255,.86)" }}>CLI / daemon / MCP 三入口共用同一引擎</span>
          </div>
        </div>
      </div>

      {/* 右：表单 */}
      <div
        className="flex shrink-0 flex-col items-center justify-center"
        style={{ width: 560, padding: 64, background: "var(--surface)" }}
      >
        <form onSubmit={onSubmit} className="flex w-[360px] flex-col gap-md">
          <div className="font-bold" style={{ fontSize: "var(--font-h1)" }}>
            {mode === "login" ? "登录 Skill OS" : "创建账号"}
          </div>
          <div className="text-[13px]" style={{ color: "var(--text-secondary)" }}>
            控制台用于查看漂移矩阵与下发同步任务
          </div>

          <label className="flex flex-col gap-xs">
            <span
              className="text-[var(--font-caption)] font-medium"
              style={{ color: "var(--text-secondary)" }}
            >
              邮箱
            </span>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="h-10 rounded-[var(--radius-control)] border px-sm font-mono outline-none"
              style={{
                borderColor: "var(--border)",
                background: "var(--surface-alt)",
                fontSize: "var(--font-mono)",
              }}
              autoComplete="email"
            />
          </label>

          <label className="flex flex-col gap-xs">
            <span
              className="text-[var(--font-caption)] font-medium"
              style={{ color: "var(--text-secondary)" }}
            >
              密码（≥8 位）
            </span>
            <input
              type="password"
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="h-10 rounded-[var(--radius-control)] border px-sm font-mono outline-none"
              style={{
                borderColor: "var(--border)",
                background: "var(--surface-alt)",
                fontSize: "var(--font-mono)",
              }}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
            />
          </label>

          {error && (
            <p
              className="text-[var(--font-caption)]"
              style={{ color: "var(--danger)" }}
            >
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy}
            className="h-11 rounded-[var(--radius-control)] px-lg font-medium text-[var(--inverse)] disabled:opacity-50"
            style={{ background: "var(--primary)" }}
          >
            {busy ? "处理中…" : mode === "login" ? "登录" : "注册并登录"}
          </button>

          <div
            className="pt-sm text-center text-[var(--font-caption)]"
            style={{ color: "var(--text-tertiary)" }}
          >
            配对 daemon 需登录后在控制台生成 6 位配对码
          </div>
        </form>
      </div>
    </div>
  );
}
