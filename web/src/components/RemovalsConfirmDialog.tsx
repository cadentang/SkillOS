/** RemovalsConfirmDialog（FR-1.0.26 / FR-2.4.7）。
 *  上游 1.34.0 事故教训（07 N29e）：列表一定很长 → 必须限高 + 内部滚动，
 *  确认/取消按钮常在屏幕内，全字号下可用。结构性约束写死在样式里，不许退化。 */

export default function RemovalsConfirmDialog({
  paths,
  onCancel,
  onConfirm,
  title = "这些文件将消失",
  description,
  confirmText = "确认删除并分发",
}: {
  paths: string[];
  onCancel: () => void;
  onConfirm: () => void;
  /** 场景化标题/文案（分发删除 vs 回滚消失共用此组件） */
  title?: string;
  description?: string;
  confirmText?: string;
}) {
  const desc =
    description ??
    `本次分发会删除目标目录中 ${paths.length} 个旧文件（新版已不含它们）。删除前 daemon 会自动备份，但请确认清单无误。`;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: "rgba(15, 23, 41, 0.45)" }}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="flex max-h-[80vh] w-[560px] flex-col rounded-[var(--radius-card)] border"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        <div className="px-lg pt-lg">
          <h2 className="font-semibold" style={{ fontSize: "var(--font-h2)" }}>
            {title}
          </h2>
          <p className="mt-xs text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
            {desc}
          </p>
        </div>

        {/* 限高 + 内部滚动：N29e 的结构性要求 */}
        <ul
          className="mx-lg mt-md flex-1 overflow-y-auto rounded-[var(--radius-control)] border p-sm"
          style={{
            borderColor: "var(--border)",
            background: "var(--surface-alt)",
            maxHeight: "40vh",
          }}
        >
          {paths.map((p) => (
            <li
              key={p}
              className="py-xs font-mono text-[var(--font-mono)]"
              style={{ color: "var(--danger)" }}
            >
              {p}
            </li>
          ))}
        </ul>

        <div
          className="flex justify-end gap-sm px-lg py-md"
          style={{ borderTop: "1px solid var(--divider)" }}
        >
          <button
            onClick={onCancel}
            className="rounded-[var(--radius-control)] border px-md py-sm"
            style={{ borderColor: "var(--border)", background: "var(--surface)" }}
          >
            取消
          </button>
          <button
            onClick={onConfirm}
            className="rounded-[var(--radius-control)] px-md py-sm font-medium text-[var(--inverse)]"
            style={{ background: "var(--danger)" }}
          >
            {confirmText}
          </button>
        </div>
      </div>
    </div>
  );
}
