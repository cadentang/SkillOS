import { useState } from "react";
import { endpoints } from "../api/endpoints";
import { ApiError } from "../api/client";

/** 新建 Skill 向导（E-07.1 ~ E-07.5）。对话框不是新页面（P3 硬约束）。
 *
 *  四步：name → description → category → 模板。分步的意义不是"仪式感"——
 *  每步都有独立的、可以在提交前就否掉的约束（kebab / 长度口径 / 模板），
 *  一次性堆在一屏里，用户只能在服务端报错之后才知道哪项不合格。
 *
 *  - name：kebab-case（与 fm.name.kebab 同口径）；重名 `NAME_CONFLICT` 与
 *    仅归一化不同 `NAME_COLLISION` 给不同提示（E-07.4）；
 *  - description：复用 fn `fm.description.length` 的 30–500 口径做实时提示，
 *    服务端仍会再跑一次 gate（E-07.5）；
 *  - 创建不自动 push（E-07.3）：成功后刷新漂移矩阵，分发走既有「一键分发」。
 */

const KEBAB = /^[a-z0-9]+(-[a-z0-9]+)*$/;
const DESC_MIN = 30;
const DESC_MAX = 500;

type Template = "minimal" | "with-deps" | "with-scripts";

const TEMPLATES: { key: Template; label: string; hint: string }[] = [
  { key: "minimal", label: "最小模板", hint: "只有 SKILL.md，最省事" },
  { key: "with-deps", label: "带依赖声明", hint: "frontmatter 含 dependencies: []" },
  { key: "with-scripts", label: "带脚本目录", hint: "多 references/ 与 scripts/ 骨架" },
];

const STEPS = ["name", "description", "category", "模板"] as const;

interface Props {
  onCancel: () => void;
  onCreated: () => void;
}

export default function CreateSkillDialog({ onCancel, onCreated }: Props) {
  const [step, setStep] = useState(0);
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState("");
  const [template, setTemplate] = useState<Template>("minimal");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const slugValid = KEBAB.test(slug);
  const descLen = description.trim().length;
  const descValid = descLen > 0;

  const stepValid =
    step === 0 ? slugValid : step === 1 ? descValid : true;

  async function submit() {
    setSubmitting(true);
    setError(null);
    try {
      const result = await endpoints.skillCreate({
        slug: slug.trim(),
        name: name.trim() || slug.trim(),
        description: description.trim(),
        category: category.trim(),
        template,
      });
      if (result.gate_has_error) {
        const first = result.gate.find((f) => f.level === "error");
        setError(
          `模板产出未通过门禁：${first ? `${first.rule} ${first.message}` : "存在 error 级问题"}`,
        );
        return;
      }
      onCreated();
    } catch (err) {
      const apiErr = err instanceof ApiError ? err : null;
      const code = apiErr?.code;
      if (code === "NAME_CONFLICT") setError(`slug 已存在：${slug.trim()}（换一个名字）`);
      else if (code === "NAME_COLLISION") setError("与已有 Skill 仅大小写不同，换一个名字（NFR-7.4）");
      else if (code === "SLUG_INVALID") setError("slug 必须是 kebab-case（小写字母数字 + 连字符）");
      else if (code === "VALIDATION") setError(apiErr?.message ?? "入参不合法");
      else if (code === "DAEMON_OFFLINE") setError("本地 daemon 离线，写操作禁用（FR-2.4.4）");
      else setError(apiErr?.message ?? String(err));
    } finally {
      setSubmitting(false);
    }
  }

  const inputCls =
    "mt-xs w-full rounded-[var(--radius-control)] border px-sm py-sm text-[var(--font-body)]";

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="新建 Skill"
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
          新建 Skill
        </h2>
        <ol className="mt-xs flex items-center gap-sm text-[var(--font-caption)]">
          {STEPS.map((label, i) => (
            <li
              key={label}
              className="flex items-center gap-xs"
              style={{ color: i === step ? "var(--primary)" : "var(--text-tertiary)" }}
            >
              <span
                className="flex h-4 w-4 items-center justify-center rounded-full text-[10px]"
                style={{
                  border: `1px solid ${i <= step ? "var(--primary)" : "var(--border)"}`,
                  color: i <= step ? "var(--primary)" : "var(--text-tertiary)",
                }}
              >
                {i + 1}
              </span>
              {label}
            </li>
          ))}
        </ol>

        <div className="mt-md min-h-[210px]">
          {step === 0 && (
            <label className="block text-[var(--font-caption)]">
              Skill 名称（kebab-case，创建后作为目录名与 slug）
              <input
                value={slug}
                onChange={(e) => setSlug(e.target.value)}
                placeholder="code-review"
                autoFocus
                className={`${inputCls} font-mono`}
                style={{
                  background: "var(--surface-alt)",
                  borderColor: slug && !slugValid ? "var(--danger)" : "var(--border)",
                }}
              />
              {slug && !slugValid && (
                <span className="mt-xs block" style={{ color: "var(--danger)" }}>
                  只允许小写字母、数字与连字符（不能以连字符开头/结尾）
                </span>
              )}
              <span className="mt-sm block">显示名（可选，缺省 = 上面的名字）</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Code Review"
                className={inputCls}
                style={{ background: "var(--surface-alt)", borderColor: "var(--border)" }}
              />
            </label>
          )}

          {step === 1 && (
            <label className="block text-[var(--font-caption)]">
              描述（描述会被其他 Agent 用来判断"什么时候用这个 Skill"）
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="这个 Skill 做什么、什么时候触发"
                rows={5}
                autoFocus
                className={`${inputCls} resize-y`}
                style={{
                  background: "var(--surface-alt)",
                  borderColor: descLen > 0 && (descLen < DESC_MIN || descLen > DESC_MAX)
                    ? "var(--warning)"
                    : "var(--border)",
                }}
              />
              <span
                className="mt-xs block tabular-nums"
                style={{
                  color: descLen > DESC_MAX
                    ? "var(--danger)"
                    : descLen > 0 && descLen < DESC_MIN
                      ? "var(--warning)"
                      : "var(--text-tertiary)",
                }}
              >
                {descLen} / {DESC_MIN}–{DESC_MAX} 字符
                {descLen > 0 && descLen < DESC_MIN ? "（偏短，门禁会提示）" : ""}
                {descLen > DESC_MAX ? "（超长，门禁会提示）" : ""}
              </span>
            </label>
          )}

          {step === 2 && (
            <label className="block text-[var(--font-caption)]">
              分类（可选，用于聚合视图分组）
              <input
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                placeholder="devops / writing / data"
                autoFocus
                className={inputCls}
                style={{ background: "var(--surface-alt)", borderColor: "var(--border)" }}
              />
              <span className="mt-sm block" style={{ color: "var(--text-tertiary)" }}>
                留空则不写 category 字段；它只影响展示分组，不影响门禁。
              </span>
            </label>
          )}

          {step === 3 && (
            <div className="flex flex-col gap-sm">
              {TEMPLATES.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  onClick={() => setTemplate(t.key)}
                  className="flex flex-col items-start gap-xs rounded-[var(--radius-control)] border px-sm py-sm text-left"
                  style={{
                    borderColor: template === t.key ? "var(--primary)" : "var(--border)",
                    background: template === t.key ? "var(--primary-soft)" : "var(--surface-alt)",
                  }}
                >
                  <span className="text-[13px] font-medium">{t.label}</span>
                  <span
                    className="text-[var(--font-caption)]"
                    style={{ color: "var(--text-secondary)" }}
                  >
                    {t.hint}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>

        {error && (
          <p
            className="mt-sm rounded-[var(--radius-control)] px-sm py-xs text-[var(--font-caption)]"
            style={{ background: "var(--surface-alt)", color: "var(--danger)" }}
          >
            {error}
          </p>
        )}

        <div className="mt-md flex items-center justify-between gap-sm">
          <span className="text-[var(--font-caption)]" style={{ color: "var(--text-tertiary)" }}>
            创建后不自动分发（E-07.3）
          </span>
          <div className="flex gap-sm">
            <button
              onClick={onCancel}
              className="rounded-[var(--radius-control)] px-md py-sm"
              style={{ background: "var(--surface-alt)", border: "1px solid var(--border)" }}
            >
              取消
            </button>
            {step > 0 && (
              <button
                onClick={() => setStep(step - 1)}
                className="rounded-[var(--radius-control)] px-md py-sm"
                style={{ background: "var(--surface-alt)", border: "1px solid var(--border)" }}
              >
                上一步
              </button>
            )}
            {step < STEPS.length - 1 ? (
              <button
                disabled={!stepValid}
                onClick={() => setStep(step + 1)}
                className="rounded-[var(--radius-control)] px-md py-sm font-medium text-[var(--inverse)] disabled:opacity-40"
                style={{ background: "var(--primary)" }}
              >
                下一步
              </button>
            ) : (
              <button
                disabled={!slugValid || !descValid || submitting}
                onClick={() => void submit()}
                className="rounded-[var(--radius-control)] px-md py-sm font-medium text-[var(--inverse)] disabled:opacity-40"
                style={{ background: "var(--primary)" }}
              >
                {submitting ? "创建中…" : "创建"}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
