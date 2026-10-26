/** 决策 T3：请求代码手写于本文件；类型从 schema.d.ts 取（npm run gen:api 生成，禁止手改）。
 *  后端契约变更后：gen:api → 看 TS 报错 → 修调用点。
 *  schema.d.ts 生成前本文件不得引用其类型（当前为初始化阶段）。 */

const TOKEN_KEY = "skillos_token";

export const tokenStore = {
  get(): string | null {
    return localStorage.getItem(TOKEN_KEY);
  },
  set(token: string) {
    localStorage.setItem(TOKEN_KEY, token);
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY);
  },
};

export class ApiError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly status: number,
    public readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

interface Envelope<T> {
  ok: boolean;
  data: T | null;
  error: { code: string; message: string; held_back_removals?: string[] } | null;
}

/** FastAPI 用 HTTPException 抛出的错误体是 `{detail: string | {code,message}}`，
 *  不走 `{ok,error}` 信封。不兜这一支，前端只能拿到 statusText ——
 *  实测日志面板把「daemon 未回包」显示成 "502 Bad Gateway"，无法判断原因。 */
function fromDetail(detail: unknown): { code: string; message: string } | null {
  if (typeof detail === "string" && detail) return { code: detail, message: detail };
  if (detail && typeof detail === "object") {
    const d = detail as { code?: unknown; message?: unknown };
    const code = typeof d.code === "string" ? d.code : null;
    const message = typeof d.message === "string" ? d.message : null;
    if (code || message) return { code: code ?? "INTERNAL", message: message ?? "" };
  }
  return null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  const token = tokenStore.get();
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(path, { ...init, headers });
  const body = (await res.json()) as Envelope<T>;
  if (!res.ok || !body.ok) {
    // 401：token 失效 → 清除并回登录页（FR-2.4.1 AuthGate）
    if (res.status === 401) {
      tokenStore.clear();
      if (window.location.pathname !== "/login") window.location.href = "/login";
    }
    const fallback = fromDetail((body as unknown as { detail?: unknown }).detail);
    throw new ApiError(
      body.error?.code ?? fallback?.code ?? "INTERNAL",
      body.error?.message || fallback?.message || res.statusText,
      res.status,
      body.error ?? fallback,
    );
  }
  return body.data as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, data?: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(data ?? {}) }),
  put: <T>(path: string, data: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(data) }),
  patch: <T>(path: string, data: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(data) }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
