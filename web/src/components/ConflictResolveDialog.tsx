import { useState } from "react";
import type { ConflictDecision } from "../api/endpoints";

/** CONFLICT 四选一（FR-2.2.6 / T22 / RED LINE 12）。
 *
 * 整文件粒度四选一（不块内合并）：
 * - keep_local  保留本地：磁盘内容成为 canonical 新版本
 * - keep_remote 保留仓库：用 canonical HEAD 覆盖 runtime 磁盘
 * - keep_both   两个都保留：磁盘 → SKILL.local-conflict.md + canonical → SKILL.md
 * - defer       暂不处理：不改任何内容，仅落 sync_records 标记 deferred
 *
 * 任一选择执行前引擎已自动建安全快照（FR-1.0.10），回滚即回到决策前。
 */

interface Props {
  slug: string;
  runtimeKey: string;
  onCancel: () => void;
  onConfirm: (decision: ConflictDecision) => void;
  submitting?: boolean;
}

const DECISIONS: Array<{
  id: ConflictDecision;
  title: string;
  detail: string;
}> = [
  {
    id: "keep_local",
    title: "保留本地",
    detail: "把磁盘当前内容回收为 canonical 新版本。runtime 改动会覆盖仓库版本。",
  },
  {
    id: "keep_remote",
    title: "保留仓库",
    detail: "用 canonical HEAD 覆盖 runtime 磁盘。磁盘本地改动会丢失。",
  },
  {
    id: "keep_both",
    title: "两个都保留",
    detail:
      "本地改动落为 *.local-conflict<ext>，canonical 写入原 basename。两份都在磁盘，互不覆盖。",
  },
  {
    id: "defer",
    title: "暂不处理",
    detail: "不改任何内容，仅落 sync_records 标记 deferred。后续再决定。",
  },
];

export default function ConflictResolveDialog({
  slug,
  runtimeKey,
  onCancel,
  onConfirm,
  submitting,
}: Props) {
  const [selected, setSelected] = useState<ConflictDecision>("keep_local");
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="解决冲突"
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: "rgba(0,0,0,0.45)" }}
      onClick={onCancel}
    >
      <div
        className="flex max-h-[85vh] w-[480px] max-w-[92vw] flex-col rounded-[var(--radius-card)] border bg-[var(--surface)] p-md shadow-xl"
        style={{ borderColor: "var(--border)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="font-semibold" style={{ fontSize: "var(--font-h2)" }}>
          解决冲突
        </h2>
        <p
          className="mt-xs text-[var(--font-caption)]"
          style={{ color: "var(--text-secondary)" }}
        >
          <span className="font-mono">{slug}</span>
          {" · "}
          <span className="font-mono">{runtimeKey}</span>
        </p>

        <ul className="mt-md flex max-h-[50vh] flex-col gap-xs overflow-y-auto pr-xs">
          {DECISIONS.map((d) => {
            const active = selected === d.id;
            return (
              <li key={d.id}>
                <label
                  className="flex cursor-pointer items-start gap-sm rounded-[var(--radius-control)] border px-sm py-sm"
                  style={{
                    borderColor: active ? "var(--primary)" : "var(--border)",
                    background: active ? "var(--surface-alt)" : "transparent",
                  }}
                >
                  <input
                    type="radio"
                    name="conflict-decision"
                    className="mt-1"
                    checked={active}
                    onChange={() => setSelected(d.id)}
                    aria-label={d.title}
                  />
                  <span>
                    <span className="font-medium">{d.title}</span>
                    <span
                      className="mt-xs block text-[var(--font-caption)]"
                      style={{ color: "var(--text-secondary)" }}
                    >
                      {d.detail}
                    </span>
                  </span>
                </label>
              </li>
            );
          })}
        </ul>

        <div
          className="mt-md rounded-[var(--radius-control)] px-sm py-xs text-[var(--font-caption)]"
          style={{ background: "var(--surface-alt)", color: "var(--text-secondary)" }}
        >
          引擎会在执行前自动建一个安全快照；任意选择都可一键回到决策前。
        </div>

        <div className="mt-md flex justify-end gap-sm">
          <button
            type="button"
            className="rounded-[var(--radius-control)] px-md py-sm"
            style={{ border: "1px solid var(--border)" }}
            onClick={onCancel}
            disabled={submitting}
          >
            取消
          </button>
          <button
            type="button"
            className="rounded-[var(--radius-control)] px-md py-sm font-medium text-[var(--inverse)] disabled:opacity-40"
            style={{ background: "var(--primary)" }}
            disabled={submitting}
            onClick={() => onConfirm(selected)}
          >
            {submitting ? "执行中…" : "确认执行"}
          </button>
        </div>
      </div>
    </div>
  );
}