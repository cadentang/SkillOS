/** 决策 T3：请求代码手写于本文件；类型从 schema.d.ts 取（npm run gen:api 生成，禁止手改）。
 *  后端契约变更后：gen:api → 看 TS 报错 → 修调用点。
 *  schema.d.ts 生成前本文件不得引用其类型（当前为初始化阶段）。 */

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
  error: { code: string; message: string; detail?: unknown } | null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  const body = (await res.json()) as Envelope<T>;
  if (!res.ok || !body.ok) {
    throw new ApiError(
      body.error?.code ?? "INTERNAL",
      body.error?.message ?? res.statusText,
      res.status,
      body.error?.detail,
    );
  }
  return body.data as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, data: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(data) }),
  put: <T>(path: string, data: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(data) }),
  patch: <T>(path: string, data: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(data) }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
