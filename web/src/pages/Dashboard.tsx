export default function Dashboard() {
  /** FR-2.1.x。组件树（04 §2.7.2）：
   *  OverviewStats（总数/分类/健康度）· DriftMatrix + DriftCellPopover（行=Skill 列=Runtime）
   *  BatchActionBar（勾选+一键分发 FR-2.1.4）· SearchBar（FR-2.1.6）
   *  P2 接入 GET /api/drift 后实现；SYNCED* 与「模板已升级」徽标规则见 FR-1.0.19/20。 */
  return (
    <div>
      <h1 className="text-[var(--font-h1)] font-semibold">Dashboard</h1>
      <p className="mt-sm text-[var(--text-secondary)]">
        漂移矩阵待接入 <code>GET /api/drift</code>（P2）。
      </p>
    </div>
  );
}
