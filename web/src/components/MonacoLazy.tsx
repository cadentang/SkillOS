import { Suspense, lazy } from "react";

/** Monaco 必须**异步加载**且 demo 主线不得依赖它（04 §2.7.5）。
 *  此文件是唯一的引入点：主 bundle 不含 monaco，编辑 tab 首次打开才拉 chunk。 */
export const MonacoEditor = lazy(() => import("./MonacoEditor"));

export function EditorFallback() {
  return (
    <div
      className="flex items-center justify-center rounded-[var(--radius-control)] border"
      style={{ height: 420, borderColor: "var(--border)", color: "var(--text-secondary)" }}
    >
      编辑器加载中…
    </div>
  );
}

export function LazyEditor(props: {
  value: string;
  language: string;
  onChange: (v: string) => void;
}) {
  return (
    <Suspense fallback={<EditorFallback />}>
      <MonacoEditor {...props} />
    </Suspense>
  );
}
