/**
 * 漂移 7 态 + 1 变体（设计稿 app/index.html §03 / Codebuddy 红线 11）。
 *
 * 设计稿颜色 + 形状双编码：色 = 状态色块，形 = 16px SVG 形状——
 * "色形同号，才不靠一种感官办事"（00-需求定稿 §0）。
 *
 * 7 态：`SYNCED` / `OUTDATED` / `MODIFIED` / `CONFLICT` / `MISSING`
 *       / `UNMANAGED` / `ORPHANED`  （22 §2.1.3，22 §FR-1.0.6）
 *
 * 1 变体：
 *   `SYNCED*` = SYNCED 但含未纳管资产（FR-1.0.20，屏幕上叠虚线外环）
 *   `PARTIAL` = 部分 Runtime 已分发（FR-2.1.9，行级聚合概念）
 *
 * 颜色令牌 `--drift-*` 与语义色 `--success/--warning/--danger`
 * 不得改名、不得改色值（Codebuddy §3 红线 11）：
 * 设计令牌唯一来源 web/src/index.css :root，已与 app/index.html 1:1 对齐。
 */

export type DriftState =
  | "SYNCED"
  | "OUTDATED"
  | "MODIFIED"
  | "CONFLICT"
  | "MISSING"
  | "UNMANAGED"
  | "ORPHANED"
  | "SYNCED_STAR" // = SYNCED* 显示变体，不是新状态；行为仍是 SYNCED
  | "PARTIAL";    // = 行级聚合（FR-2.1.9）

/** 设计稿 / 旧调用方都可能用小写 / 旧枚举 → 统一映射为 DriftState 之一。 */
export function normalizeDriftState(input: string): DriftState | string {
  if (!input) return "MISSING";
  const u = input.toUpperCase();
  if (u === "SYNCED*" || u === "SYNCED_STAR") return "SYNCED_STAR";
  return u;
}

const STATE_LABEL: Record<string, string> = {
  SYNCED: "已同步",
  SYNCED_STAR: "已同步*",
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
  SYNCED_STAR: "var(--drift-synced)",
  OUTDATED: "var(--drift-outdated)",
  MODIFIED: "var(--drift-modified)",
  CONFLICT: "var(--drift-conflict)",
  MISSING: "var(--drift-missing)",
  UNMANAGED: "var(--drift-unmanaged)",
  ORPHANED: "var(--drift-orphaned)",
  PARTIAL: "var(--warning)",
};

export function stateColor(state: string): string {
  return STATE_COLOR[normalizeDriftState(state)] ?? "var(--drift-missing)";
}

export function stateLabel(state: string): string {
  return STATE_LABEL[normalizeDriftState(state)] ?? state;
}

/** Legend 用的迷你色块（10×10 圆角 3）—— 设计稿 `.legend__dot`。
 *  Dashboard 漂移矩阵顶部右侧的「已同步 / 待分发 / 需处理」三色图例。 */
export function LegendDot({ state }: { state: string }) {
  return (
    <span
      className="inline-block shrink-0 rounded-[3px]"
      style={{ width: 10, height: 10, background: stateColor(state) }}
      aria-hidden
    />
  );
}

/** 色点 + 文本（"色"已有 + "形"是中文 label，色形双编码：色块是色，中文是形）。 */
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

/** 矩阵格 chip（保留现有调用方使用）：色块 + 中文 label。 */
export function StateCell({ state, title }: { state: string; title?: string }) {
  return (
    <span
      title={title ?? stateLabel(state)}
      className="inline-flex items-center justify-center rounded-[var(--radius-tag)] px-sm py-xs text-[var(--font-caption)] text-[var(--inverse)] whitespace-nowrap"
      style={{ background: stateColor(state) }}
    >
      {stateLabel(state)}
    </span>
  );
}

/** ─────────────────────────────────────────────────────────────────
 *  DriftMark — 16×16 SVG 形状，颜色 + 形状双编码（设计稿 §04）。
 *  与 chip 不同：DriftMark 把"形"画出来，是色 + 形的最小可视单元。
 *
 *  设计稿定义（与 app/index.html §04 1:1 对齐）：
 *   SYNCED    = 实心圆
 *   SYNCED*   = 实心圆 + 虚线外环（叠层，不改色）
 *   OUTDATED  = 上箭头（圆点角变尖）
 *   MODIFIED  = 同心圆（外环 + 内实心）
 *   CONFLICT  = 内外双圆（环 + 内填）
 *   MISSING   = 描边圆（无线色中空）
 *   UNMANAGED = 虚线方块
 *   ORPHANED  = 斜线（删除号）
 *   PARTIAL   = 半填充圆（半 SYNCED + 半 OUTDATED 视觉含义）
 *  ───────────────────────────────────────────────────────────────── */

interface GlyphProps {
  state: string;
  size?: number;
  title?: string;
}

export function DriftMark({ state, size = 16, title }: GlyphProps) {
  const ns = stateColor(state); // 形状内的颜色（白色外圈 + 状态色块背景）
  const n = normalizeDriftState(state);
  const label = title ?? stateLabel(state);

  return (
    <span
      className="inline-flex shrink-0 items-center justify-center"
      style={{ width: size, height: size }}
      title={label}
      aria-label={label}
    >
      <svg
        width={size}
        height={size}
        viewBox="0 0 16 16"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
      >
        {n === "SYNCED" && (
          <circle cx="8" cy="8" r="4.5" fill={ns} />
        )}
        {n === "SYNCED_STAR" && (
          <>
            {/* 外虚线环（叠层） */}
            <circle cx="8" cy="8" r="6.5" stroke={ns} strokeWidth="1.4" strokeDasharray="2.2 2.2" />
            {/* 内实心圆 */}
            <circle cx="8" cy="8" r="3.5" fill={ns} />
          </>
        )}
        {n === "OUTDATED" && (
          <path d="M8 12.5V3.5M4 7.5L8 3.5L12 7.5" stroke={ns} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        )}
        {n === "MODIFIED" && (
          <>
            <circle cx="8" cy="8" r="5" fill={ns} />
            <circle cx="8" cy="8" r="2" fill="#fff" />
          </>
        )}
        {n === "CONFLICT" && (
          <>
            <circle cx="8" cy="8" r="6.5" stroke={ns} strokeWidth="1.6" />
            <circle cx="8" cy="8" r="3.2" fill={ns} />
          </>
        )}
        {n === "MISSING" && (
          <circle cx="8" cy="8" r="4.5" stroke={ns} strokeWidth="1.8" />
        )}
        {n === "UNMANAGED" && (
          <rect x="3" y="3" width="10" height="10" rx="2.5" stroke={ns} strokeWidth="1.6" strokeDasharray="2.4 2" />
        )}
        {n === "ORPHANED" && (
          <path d="M4 12L12 4" stroke={ns} strokeWidth="2" strokeLinecap="round" />
        )}
        {n === "PARTIAL" && (
          <>
            {/* 半 SYNCED + 半 OUTDATED 的视觉含义：实心半圆 + 描边半圆 */}
            <circle cx="8" cy="8" r="5" fill={ns} fillOpacity="0.45" />
            <path d="M8 3 a5 5 0 0 1 0 10 z" fill={ns} />
            <circle cx="8" cy="8" r="5" stroke={ns} strokeWidth="1.2" fill="none" />
          </>
        )}
      </svg>
    </span>
  );
}

/** 设计稿 `.cell` = 44×44 圆角 8 白底 + 状态色形（最小可视单元）。
 *  用于漂移矩阵 DriftMatrix 的格子里（step 4 才使用，本文件先导出）。 */
export function DriftCell({
  state,
  title,
  size = 44,
}: {
  state: string;
  title?: string;
  size?: number;
}) {
  return (
    <span
      className="inline-flex items-center justify-center rounded-[var(--radius-control)] border"
      style={{
        width: size,
        height: size,
        background: "var(--surface)",
        borderColor: "var(--border)",
      }}
      title={title ?? stateLabel(state)}
    >
      <DriftMark state={state} size={18} />
    </span>
  );
}
