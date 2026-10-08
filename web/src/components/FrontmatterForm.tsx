import { useEffect, useState } from "react";

/** FrontmatterForm（FR-2.2.1）：SKILL.md 头部元数据表单。
 *  序列化口径：标准字段 name/description/version 置顶，未知字段原样透传（OUT-9）。
 *  不引 yaml 库 —— 只处理 `key: value` 平面形态（复杂值交给编辑器 tab）。 */

export interface Frontmatter {
  name: string;
  description: string;
  version: string;
}

export function splitFrontmatter(text: string): {
  fm: Frontmatter;
  extraLines: string[];
  body: string;
} {
  const lines = text.split("\n");
  const fm: Frontmatter = { name: "", description: "", version: "" };
  const extraLines: string[] = [];
  let inFm = false;
  let bodyStart = 0;
  let closed = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!inFm) {
      if (line.trim() === "---") {
        inFm = true;
        continue;
      }
      break;
    }
    if (line.trim() === "---") {
      closed = true;
      bodyStart = i + 1;
      break;
    }
    const m = line.match(/^([A-Za-z_-]+):\s*(.*)$/);
    if (m) {
      const key = m[1];
      const raw = m[2].replace(/^["']|["']$/g, "");
      if (key === "name") fm.name = raw;
      else if (key === "description") fm.description = raw;
      else if (key === "version") fm.version = raw;
      else extraLines.push(line);
    } else {
      extraLines.push(line);
    }
  }
  const body = closed ? lines.slice(bodyStart).join("\n") : text;
  return { fm, extraLines, body };
}

export function joinFrontmatter(
  fm: Frontmatter,
  extraLines: string[],
  body: string,
): string {
  const parts = ["---"];
  parts.push(`name: ${fm.name}`);
  parts.push(`version: ${fm.version}`);
  // description 含冒号/引号时加引号转义，避免 yaml 解析歧义
  const desc = /[:#"'{}\[\]]/.test(fm.description)
    ? `"${fm.description.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`
    : fm.description;
  parts.push(`description: ${desc}`);
  parts.push(...extraLines);
  parts.push("---");
  return `${parts.join("\n")}\n${body}`;
}

export default function FrontmatterForm({
  initial,
  disabled,
  onSave,
}: {
  initial: string; // 完整 SKILL.md 内容
  disabled?: boolean;
  onSave: (next: string) => void;
}) {
  const { fm, extraLines, body } = splitFrontmatter(initial);
  const [name, setName] = useState(fm.name);
  const [description, setDescription] = useState(fm.description);
  const [version, setVersion] = useState(fm.version);

  useEffect(() => {
    setName(fm.name);
    setDescription(fm.description);
    setVersion(fm.version);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initial]);

  const dirty =
    name !== fm.name || description !== fm.description || version !== fm.version;

  const inputStyle = {
    borderColor: "var(--border)",
    background: "var(--surface)",
  };

  return (
    <div
      className="rounded-[var(--radius-control)] border p-md"
      style={{ borderColor: "var(--border)", background: "var(--surface-alt)" }}
    >
      <div className="grid grid-cols-2 gap-md">
        <label className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
          name（kebab-case）
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="mt-xs w-full rounded-[var(--radius-control)] border px-sm py-sm"
            style={inputStyle}
          />
        </label>
        <label className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
          version
          <input
            value={version}
            onChange={(e) => setVersion(e.target.value)}
            className="mt-xs w-full rounded-[var(--radius-control)] border px-sm py-sm"
            style={inputStyle}
          />
        </label>
      </div>
      <label className="mt-md block text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
        description（{description.length} 字 · 门禁要求 30–500，FR-1.0.11）
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={3}
          className="mt-xs w-full rounded-[var(--radius-control)] border px-sm py-sm"
          style={inputStyle}
        />
      </label>
      <button
        disabled={disabled || !dirty}
        onClick={() => onSave(joinFrontmatter({ name, description, version }, extraLines, body))}
        className="mt-md rounded-[var(--radius-control)] px-md py-sm font-medium text-[var(--inverse)] disabled:opacity-40"
        style={{ background: "var(--primary)" }}
      >
        保存元数据{dirty ? "" : "（无变更）"}
      </button>
    </div>
  );
}
