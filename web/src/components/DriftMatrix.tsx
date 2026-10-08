import { Link } from "react-router-dom";
import type { DriftRow } from "../api/endpoints";
import { StateCell, stateLabel } from "./StateChip";

/** 四端漂移矩阵（FR-2.1.2）：行 = Skill（聚合态 FR-2.1.9），列 = Runtime，格 = 状态。
 *  两类徽标（FR-1.0.19/20）：●模板已升级 / ▲含未纳管资产（live 数据才有，缓存视图无）。
 *  行勾选供 BatchActionBar 一键分发（FR-2.1.4）。 */

const RUNTIME_ORDER = ["claude", "agents", "workbuddy", "codex"];

export function runtimeColumns(rows: DriftRow[]): string[] {
  const seen = new Set<string>();
  for (const row of rows) Object.keys(row.cells).forEach((k) => seen.add(k));
  return RUNTIME_ORDER.filter((k) => seen.has(k)).concat(
    [...seen].filter((k) => !RUNTIME_ORDER.includes(k)).sort(),
  );
}

export default function DriftMatrix({
  rows,
  selected,
  onToggle,
  onToggleAll,
  onResolveConflict,
}: {
  rows: DriftRow[];
  selected: Set<string>;
  onToggle: (slug: string) => void;
  onToggleAll: (checked: boolean) => void;
  /** 单元格为可选：CONFLICT 态的 (slug, runtime_key) 点击回调（P3）。*/
  onResolveConflict?: (slug: string, runtimeKey: string) => void;
}) {
  const columns = runtimeColumns(rows);
  if (rows.length === 0) {
    return (
      <p className="py-lg text-center" style={{ color: "var(--text-secondary)" }}>
        暂无数据 —— 请先在本机运行 <code>skillos init && skillos import</code> 并启动 daemon。
      </p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[var(--font-body)]">
        <thead>
          <tr style={{ borderBottom: "1px solid var(--border)" }}>
            <th className="w-10 py-sm pr-xs text-left">
              <input
                type="checkbox"
                aria-label="全选"
                checked={selected.size === rows.length && rows.length > 0}
                onChange={(e) => onToggleAll(e.target.checked)}
              />
            </th>
            <th className="py-sm text-left font-medium">Skill</th>
            <th className="py-sm text-left font-medium">状态</th>
            {columns.map((c) => (
              <th key={c} className="py-sm text-center font-medium">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.slug}
              style={{ borderBottom: "1px solid var(--divider)", height: "var(--row-h)" }}
            >
              <td className="pr-xs">
                <input
                  type="checkbox"
                  aria-label={`选择 ${row.slug}`}
                  checked={selected.has(row.slug)}
                  onChange={() => onToggle(row.slug)}
                />
              </td>
              <td className="py-xs">
                <Link
                  to={`/skills/${encodeURIComponent(row.slug)}`}
                  className="font-medium no-underline"
                  style={{ color: "var(--primary)" }}
                >
                  {row.slug}
                </Link>
              </td>
              <td className="py-xs">
                <StateCell state={row.state} />
              </td>
              {columns.map((col) => {
                const cell = row.cells[col];
                const isConflict = cell?.state === "CONFLICT";
                return (
                  <td key={col} className="py-xs text-center">
                    {cell ? (
                      <span className="inline-flex items-center justify-center gap-xs">
                        {isConflict && onResolveConflict ? (
                          <button
                            type="button"
                            className="cursor-pointer rounded-[var(--radius-tag)] border-0 bg-transparent p-0"
                            title={`${col}: 冲突 —— 点击解决（FR-2.2.6）`}
                            onClick={() => onResolveConflict(row.slug, col)}
                          >
                            <StateCell state={cell.state} />
                          </button>
                        ) : (
                          <StateCell
                            state={cell.state}
                            title={`${col}: ${stateLabel(cell.state)}`}
                          />
                        )}
                        {cell.adapter_outdated && (
                          <span
                            title="模板已升级（FR-1.0.19）：Runtime 模板有新版本"
                            style={{ color: "var(--badge-adapter)" }}
                          >
                            ●
                          </span>
                        )}
                        {cell.has_untracked_assets && (
                          <span
                            title="含未纳管资产（FR-1.0.20）：存在超阈值大文件，不参与分发"
                            style={{ color: "var(--warning)" }}
                          >
                            ▲
                          </span>
                        )}
                      </span>
                    ) : (
                      <span style={{ color: "var(--text-tertiary)" }}>—</span>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
