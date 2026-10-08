import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import Login from "./pages/Login";
import SkillDetail from "./pages/SkillDetail";
import Teams from "./pages/Teams";
import { tokenStore } from "./api/client";

/** AuthGate（FR-2.4.1）：无 token 一律去 /login；受保护页统一在此收口。 */
function AuthGate({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  if (!tokenStore.get()) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <AuthGate>
            <Layout />
          </AuthGate>
        }
      >
        <Route path="/" element={<Dashboard />} />
        <Route path="/skills" element={<Navigate to="/" replace />} />
        <Route path="/skills/:slug" element={<SkillDetail />} />
        <Route path="/teams" element={<Teams />} />
      </Route>
    </Routes>
  );
}
