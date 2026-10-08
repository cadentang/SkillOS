import { useState } from "react";
import { endpoints } from "../api/endpoints";
import { ApiError } from "../api/client";

/** 新建 Skill（FR-2.1.7 / P3）。对话框不是新页面（P3 硬约束：不新增页面与 tab）。
 *
 * - slug：kebab-case（与 fm.name.kebab 同口径），前端快速校验 + 后端全量校验
 * - description：必填（fm.description.required 为 error 级门禁）
 * - 创建不自动 push：成功后刷新漂移矩阵，分发走既有「一键分发」
 */

const KEBAB = /^[a-z0-9]+(-[a-z0-9]+)*$/;

interface Props {
  onCancel: () => void;
  onCreated: () => void;
}

export default function CreateSkillDialog({ onCancel, onCreated }: Props) {
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const slugValid = KEBAB.test(slug);
  const canSubmit = slugValid && description.trim().length > 0 && !submitting;

  async function submit() {
    setSubmitting(true);
    setError(null);
    try {
      const result = await endpoints.skillCreate(
        slug.trim(),
        name.trim() || slug.trim(),
        description.trim(),
      );
      if (result.gate_has_error) {
        // 引擎侧已拒绝 error 级前置（name/description 必填）；此分支仅防御
        setError("门禁 error 级未通过，请检查输入");
        return;
      }
      onCreated();
    } catch (err) {
      const apiErr = err instanceof ApiError ? err : null;
      const code = apiErr?.code;
      if (code === "SLUG_EXISTS") setError(`slug 已存在：${slug.trim()}`);
      else if (code === "SLUG_INVALID") setError("slug 必须是 kebab-case（小写字母数字 + 连字符）");
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
        <p
          className="mt-xs text-[var(--font-caption)]"
          style={{ color: "var(--text-secondary)" }}
        >
          在 canonical 仓库创建模板并提交；不自动分发，创建后可用「一键分发」。
        </p>

        <label className="mt-md block text-[var(--font-caption)]">
          slug（kebab-case，必填）
          <input
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="code-review"
            className={`${inputCls} font-mono`}
            style={{
              background: "var(--surface-alt)",
              borderColor: slug && !slugValid ? "var(--danger)" : "var(--border)",
            }}
          />
          {slug && !slugValid && (
            <span className="mt-xs block" style={{ color: "var(--danger)" }}>
              只允许小写字母、数字与连字符
            </span>
          )}
        </label>

        <label className="mt-md block text-[var(--font-caption)]">
          显示名（可选，缺省 = slug）
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Code Review"
            className={inputCls}
            style={{ background: "var(--surface-alt)", borderColor: "var(--border)" }}
          />
        </label>

        <label className="mt-md block text-[var(--font-caption)]">
          描述（必填，建议 30–500 字符）
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="这个 Skill 做什么、什么时候触发"
            rows={3}
            className={`${inputCls} resize-y`}
            style={{ background: "var(--surface-alt)", borderColor: "var(--border)" }}
          />
        </label>

        {error && (
          <p
            className="mt-sm rounded-[var(--radius-control)] px-sm py-xs text-[var(--font-caption)]"
            style={{ background: "var(--surface-alt)", color: "var(--danger)" }}
          >
            {error}
          </p>
        )}

        <div className="mt-md flex justify-end gap-sm">
          <button
            onClick={onCancel}
            className="rounded-[var(--radius-control)] px-md py-sm"
            style={{ background: "var(--surface-alt)", border: "1px solid var(--border)" }}
          >
            取消
          </button>
          <button
            disabled={!canSubmit}
            onClick={() => void submit()}
            className="rounded-[var(--radius-control)] px-md py-sm font-medium text-[var(--inverse)] disabled:opacity-40"
            style={{ background: "var(--primary)" }}
          >
            {submitting ? "创建中…" : "创建"}
          </button>
        </div>
      </div>
    </div>
  );
}
