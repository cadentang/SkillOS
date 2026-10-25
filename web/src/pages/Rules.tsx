import { useQuery } from "@tanstack/react-query";
import {
  endpoints,
  type CustomRuleItem,
  type IgnoreRuleItem,
  type InvalidRuleItem,
} from "../api/endpoints";
import { ApiError } from "../api/client";

/** 规则面板（M3-05 / N-03 / N-08.2）。
 *
 *  数据全在 daemon 本地（用户在仓库 `.skillos/rules/` 里维护的文件），云端只代理
 *  读取（红线 5：不碰磁盘）。三种「不正常」必须分开显示，不能合并成一句「加载失败」：
 *  - `invalid`：文件在、规则语法/前缀不合格 —— 需要用户改文件（N-03.4 不静默丢弃）；
 *  - `ignores`：白名单放行的规则 —— 展示理由，否则等于开了后门没人知道（N-08.2）；
 *  - 空 `rules` + 空 `files`：还没拷贝预置规则集（首启由 daemon 完成）。
 */

const LEVEL_COLOR: Record<CustomRuleItem["level"], string> = {
  error: "var(--danger)",
  warn: "var(--warning)",
  info: "var(--text-secondary)",
};

export default function Rules() {
  const panel = useQuery({
    queryKey: ["rules"],
    queryFn: endpoints.rules,
    retry: false,
  });

  const err = panel.error;
  const offline = err instanceof ApiError && err.code === "DAEMON_OFFLINE";
  const data = panel.data;

  return (
    <div className="flex h-full flex-col gap-md">
      <div className="flex items-baseline justify-between">
        <h1 className="font-semibold" style={{ fontSize: "var(--font-h1)" }}>
          规则面板
        </h1>
        <button
          type="button"
          onClick={() => void panel.refetch()}
          className="rounded-[var(--radius-control)] px-sm py-xs text-[var(--font-caption)]"
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            color: "var(--text-secondary)",
          }}
        >
          刷新
        </button>
      </div>

      {offline && (
        <p
          className="rounded-[var(--radius-control)] px-sm py-sm text-[var(--font-caption)]"
          style={{ color: "var(--warning)", background: "var(--surface-alt)" }}
        >
          daemon 离线：规则文件在仓库
          <code className="mx-xs">.skillos/rules/</code>
          里，连上后才能读取。
        </p>
      )}
      {panel.isError && !offline && (
        <p className="text-[13px]" style={{ color: "var(--danger)" }}>
          读取失败：{err instanceof Error ? err.message : String(err)}
        </p>
      )}

      {data && (
        <>
          <p className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
            规则集 {data.ruleset_hash ? data.ruleset_hash.slice(0, 12) : "（空）"} ·
            生效 {data.rules.length} 条 · 无效 {data.invalid.length} 条 · 白名单{" "}
            {data.ignores.length} 条
            {data.files.length > 0 ? ` · 来源 ${data.files.length} 个文件` : ""}
          </p>

          <Panel title="① 生效规则">
            {data.rules.length === 0 ? (
              <Muted text="没有生效的自定义规则。预置安全规则集由 daemon 首启拷贝到 .skillos/rules/。" />
            ) : (
              <div className="flex flex-col">
                {data.rules.map((rule) => (
                  <RuleRow key={`${rule.source}:${rule.rule_id}`} rule={rule} />
                ))}
              </div>
            )}
          </Panel>

          <Panel title={`② 无效规则（${data.invalid.length}）`}>
            {/* 无效规则必须显式暴露原因：静默丢弃会让人以为规则生效了 */}
            {data.invalid.length === 0 ? (
              <Muted text="全部规则编译通过。" />
            ) : (
              data.invalid.map((item) => (
                <InvalidRow key={`${item.source}:${item.rule_id}`} item={item} />
              ))
            )}
          </Panel>

          <Panel title={`③ ignore 白名单（${data.ignores.length}）`}>
            {data.ignores.length === 0 ? (
              <Muted text="没有白名单条目，所有命中都会落 finding。" />
            ) : (
              data.ignores.map((item, index) => (
                <IgnoreRow key={`${item.rule_id}:${index}`} item={item} />
              ))
            )}
          </Panel>
        </>
      )}

      {panel.isLoading && <p className="text-[13px]">加载中…</p>}
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div
      className="flex flex-col gap-sm rounded-[var(--radius-card)] border p-md"
      style={{ borderColor: "var(--border)", background: "var(--surface)" }}
    >
      <span className="font-semibold text-[13px]">{title}</span>
      {children}
    </div>
  );
}

function RuleRow({ rule }: { rule: CustomRuleItem }) {
  const scope = [...rule.scope_paths, ...rule.scope_exclude.map((p) => `!${p}`)];
  return (
    <div
      className="flex flex-col gap-xs border-b py-sm"
      style={{ borderColor: "var(--divider)" }}
    >
      <span className="flex flex-wrap items-baseline gap-sm">
        <code className="text-[13px] font-medium">{rule.rule_id}</code>
        <Badge text={rule.level} color={LEVEL_COLOR[rule.level]} />
        <span className="text-[var(--font-caption)]" style={{ color: "var(--text-tertiary)" }}>
          {rule.target}
          {rule.category ? ` · ${rule.category}` : ""}
        </span>
      </span>
      <code
        className="break-all text-[var(--font-caption)]"
        style={{ color: "var(--text-secondary)" }}
      >
        /{rule.pattern}/{rule.flags.join("")}
      </code>
      {rule.message && (
        <span className="text-[var(--font-caption)]" style={{ color: "var(--text-secondary)" }}>
          {rule.message}
        </span>
      )}
      <span className="flex flex-wrap gap-sm text-[var(--font-caption)]"
            style={{ color: "var(--text-tertiary)" }}>
        {scope.length > 0 ? <span>范围 {scope.join(" ")}</span> : <span>范围 全仓库</span>}
        <span>来源 {rule.source}</span>
      </span>
    </div>
  );
}

function InvalidRow({ item }: { item: InvalidRuleItem }) {
  return (
    <div className="flex flex-col gap-xs border-b py-sm" style={{ borderColor: "var(--divider)" }}>
      <span className="flex flex-wrap items-baseline gap-sm">
        <code className="text-[13px]">{item.rule_id || "（无 rule_id）"}</code>
        <Badge text="invalid" color="var(--danger)" />
      </span>
      <span className="text-[var(--font-caption)]" style={{ color: "var(--danger)" }}>
        {item.reason}
      </span>
      <span className="text-[var(--font-caption)]" style={{ color: "var(--text-tertiary)" }}>
        来源 {item.source}
      </span>
    </div>
  );
}

function IgnoreRow({ item }: { item: IgnoreRuleItem }) {
  return (
    <div className="flex flex-col gap-xs border-b py-sm" style={{ borderColor: "var(--divider)" }}>
      <code className="text-[13px]">{item.rule_id}</code>
      <span className="break-all text-[var(--font-caption)]"
            style={{ color: "var(--text-secondary)" }}>
        {item.paths.join(" · ")}
      </span>
      <span className="text-[var(--font-caption)]" style={{ color: "var(--text-tertiary)" }}>
        理由 {item.reason || "（未填写）"}
      </span>
    </div>
  );
}

function Badge({ text, color }: { text: string; color: string }) {
  return (
    <span
      className="rounded-[var(--radius-control)] px-xs text-[var(--font-caption)]"
      style={{ color, border: `1px solid ${color}` }}
    >
      {text}
    </span>
  );
}

function Muted({ text }: { text: string }) {
  return (
    <p className="text-[var(--font-caption)]" style={{ color: "var(--text-tertiary)" }}>
      {text}
    </p>
  );
}
