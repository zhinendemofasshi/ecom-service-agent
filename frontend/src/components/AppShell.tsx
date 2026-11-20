import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { cn } from "@/lib/cn";

export function AppShell() {
  const { identity, signOut } = useAuth();
  const link = ({ isActive }: { isActive: boolean }) =>
    cn("rounded-lg px-3 py-1.5 text-sm transition", isActive ? "bg-brand-soft text-brand font-medium" : "text-muted hover:text-ink");

  return (
    <div className="flex h-dvh flex-col">
      <header className="flex h-14 shrink-0 items-center gap-4 border-b border-line bg-surface px-4">
        <div className="flex items-center gap-2 font-semibold">
          <span className="grid size-7 place-items-center rounded-lg bg-brand text-sm text-brand-ink">夕</span>
          <span className="hidden sm:inline">小夕客服</span>
        </div>
        <nav className="flex gap-1">
          <NavLink to="/chat" className={link}>
            对话
          </NavLink>
          <NavLink to="/approvals" className={link}>
            审批工作台
          </NavLink>
        </nav>
        <div className="ml-auto flex items-center gap-3 text-sm">
          <span className="hidden text-muted md:inline">
            {identity?.actor_id}
            {identity?.can_approve && " · 审核员"}
          </span>
          <button className="text-muted hover:text-ink" onClick={() => void signOut()}>
            退出
          </button>
        </div>
      </header>
      <div className="min-h-0 flex-1">
        <Outlet />
      </div>
    </div>
  );
}
