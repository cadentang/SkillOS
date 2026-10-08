import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { endpoints } from "../api/endpoints";
import { tokenStore } from "../api/client";

/** 登录 / 注册（FR-2.4.1 AuthGate 的入口）。demo 用户一次性注册即可。 */
export default function Login() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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
    <div
      className="flex min-h-screen items-center justify-center"
      style={{ background: "var(--canvas)" }}
    >
      <form
        onSubmit={onSubmit}
        className="w-[380px] rounded-[var(--radius-card)] border p-lg"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        <h1 className="font-semibold" style={{ fontSize: "var(--font-h1)" }}>
          Skill OS
        </h1>
        <p className="mt-xs" style={{ color: "var(--text-secondary)" }}>
          {mode === "login" ? "登录控制台" : "创建账号"}
        </p>

        <label className="mt-lg block text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
          邮箱
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="mt-xs w-full rounded-[var(--radius-control)] border px-sm py-sm"
            style={{ borderColor: "var(--border)", background: "var(--surface)" }}
          />
        </label>
        <label className="mt-md block text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
          密码（≥8 位）
          <input
            type="password"
            required
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-xs w-full rounded-[var(--radius-control)] border px-sm py-sm"
            style={{ borderColor: "var(--border)", background: "var(--surface)" }}
          />
        </label>

        {error && (
          <p className="mt-sm text-[var(--font-caption)]" style={{ color: "var(--danger)" }}>
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy}
          className="mt-lg w-full rounded-[var(--radius-control)] py-sm font-medium text-[var(--inverse)] disabled:opacity-50"
          style={{ background: "var(--primary)" }}
        >
          {busy ? "处理中…" : mode === "login" ? "登录" : "注册并登录"}
        </button>
        <button
          type="button"
          className="mt-sm w-full py-xs text-[var(--font-caption)]"
          style={{ color: "var(--text-secondary)" }}
          onClick={() => setMode(mode === "login" ? "register" : "login")}
        >
          {mode === "login" ? "没有账号？注册" : "已有账号？登录"}
        </button>
      </form>
    </div>
  );
}
