export default function SkillDetail() {
  /** FR-2.2.x。Tab 结构（04 §2.7.2）：
   *  MetaPanel · Files(FileTree + MonacoEditor 动态 import + FrontmatterForm)
   *  Diff(ConflictResolver 整文件三选一) · Versions(时间轴+回滚) · Checks
   *  Monaco 必须异步加载且 demo 主线不得依赖它（04 §2.7.5）。 */
  return (
    <div>
      <h1 className="text-[var(--font-h1)] font-semibold">Skill Detail</h1>
      <p className="mt-sm text-[var(--text-secondary)]">详情页待接入 REST / WS（P2）。</p>
    </div>
  );
}
