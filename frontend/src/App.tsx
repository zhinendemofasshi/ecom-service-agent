import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import { useAuth } from "@/auth/AuthContext";
import { LoginPage } from "@/auth/LoginPage";
import { AppShell } from "@/components/AppShell";
import { ChatPage } from "@/features/chat/ChatPage";
import { ApprovalsPage } from "@/features/approvals/ApprovalsPage";

function RequireAuth({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "loading") {
    return <div className="grid h-dvh place-items-center text-sm text-muted">正在验证登录状态…</div>;
  }
  if (status === "anonymous") return <Navigate to="/login" replace state={{ from: location }} />;
  return <>{children}</>;
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/chat/:sessionId" element={<ChatPage />} />
        <Route path="/approvals" element={<ApprovalsPage />} />
        <Route path="/approvals/:sessionId" element={<ApprovalsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/chat" replace />} />
    </Routes>
  );
}
