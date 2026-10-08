/** 漂移状态 → 令牌色映射（--drift-* 不得改名改值，index.css 注释）。 */

const STATE_LABEL: Record<string, string> = {
  SYNCED: "已同步",
  OUTDATED: "有新版本",
  MODIFIED: "本地已改",
  CONFLICT: "冲突",
  MISSING: "待分发",
  UNMANAGED: "未纳管",
  ORPHANED: "孤儿",
  PARTIAL: "部分分发",
};

const STATE_COLOR: Record<string, string> = {
  SYNCED: "var(--drift-synced)",
  OUTDATED: "var(--drift-outdated)",
  MODIFIED: "var(--drift-modified)",
  CONFLICT: "var(--drift-conflict)",
  MISSING: "var(--drift-missing)",
  UNMANAGED: "var(--drift-unmanaged)",
  ORPHANED: "var(--drift-orphaned)",
  PARTIAL: "var(--drift-modified)",
};

export function stateColor(state: string): string {
  return STATE_COLOR[state] ?? "var(--drift-missing)";
}

export function stateLabel(state: string): string {
  return STATE_LABEL[state] ?? state;
}

/** 色点 + 文本（色 + 形双编码：文本即形，不只靠颜色）。 */
export function StateDot({ state }: { state: string }) {
  return (
    <span className="inline-flex items-center gap-xs whitespace-nowrap">
      <span
        className="inline-block h-2 w-2 rounded-full"
        style={{ background: stateColor(state) }}
      />
      {stateLabel(state)}
    </span>
  );
}

/** 矩阵格：色块 + 状态缩写；两类徽标（FR-1.0.19/20）。 */
export function StateCell({ state, title }: { state: string; title?: string }) {
  return (
    <span
      title={title ?? stateLabel(state)}
      className="inline-flex items-center justify-center rounded-[var(--radius-tag)] px-sm py-xs text-[var(--font-caption)] text-[var(--inverse)]"
      style={{ background: stateColor(state), opacity: state === "SYNCED" ? 0.9 : 1 }}
    >
      {state.slice(0, 3)}
    </span>
  );
}
