import { useEffect, useRef } from "react";
import Editor, { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
import editorWorker from "monaco-editor/editor/editor.worker?worker";
import jsonWorker from "monaco-editor/language/json/json.worker.js?worker";

/** Monaco 实体（仅经 MonacoLazy 异步引入）。
 *  worker 本地打包（不走 CDN）：投屏 demo 不依赖外网（03 §8）。 */

let configured = false;

function configure() {
  if (configured) return;
  configured = true;
  (self as unknown as { MonacoEnvironment?: unknown }).MonacoEnvironment = {
    getWorker(_workerId: string, label: string) {
      if (label === "json") return new jsonWorker();
      return new editorWorker();
    },
  };
  loader.config({ monaco });
}

export default function MonacoEditor({
  value,
  language,
  onChange,
}: {
  value: string;
  language: string;
  onChange: (v: string) => void;
}) {
  configure();
  const holder = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // monaco 自带焦点样式会盖掉设计令牌，这里只对齐字体
    if (holder.current) {
      holder.current.style.fontFamily = "var(--font-code)";
    }
  }, []);

  return (
    <div ref={holder} className="min-h-0 flex-1 overflow-hidden rounded-[var(--radius-control)] border"
         style={{ borderColor: "var(--border)" }}>
      <Editor
        height="100%"
        language={language}
        value={value}
        theme="vs"
        options={{
          minimap: { enabled: false },
          fontSize: 13,
          automaticLayout: true,
          scrollBeyondLastLine: false,
          tabSize: 2,
        }}
        onChange={(v) => onChange(v ?? "")}
      />
    </div>
  );
}
