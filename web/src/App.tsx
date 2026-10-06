import { Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import SkillDetail from "./pages/SkillDetail";
import Teams from "./pages/Teams";

/** 页面与组件树见 04 §2.7.2。AuthGate（FR-2.4.1）待云端认证接入后补齐。 */
export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/skills/:id" element={<SkillDetail />} />
        <Route path="/teams" element={<Teams />} />
      </Route>
    </Routes>
  );
}
