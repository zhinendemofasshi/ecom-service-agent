import { useState } from "react";
import type { FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { api } from "@/api/client";
import { useAuth } from "./AuthContext";
import { Button } from "@/components/ui";

export function LoginPage() {
  const { status, signIn } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const config = useQuery({ queryKey: ["auth-config"], queryFn: api.authConfig });

  if (status === "authenticated") return <Navigate to="/chat" replace />;
  const from = (location.state as { from?: { pathname: string } } | null)?.from?.pathname;

  async function finish(t: string, fallback: string) {
    const me = await signIn(t);
    navigate(from ?? (me.can_approve ? "/approvals" : fallback), { replace: true });
  }

  async function demo(role: "customer" | "approver") {
    setError("");
    setBusy(role);
    try {
      const result = await api.demoLogin(role);
      await finish(result.token, role === "approver" ? "/approvals" : "/chat");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy("token");
    try {
      await finish(token.trim(), "/chat");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <main className="grid min-h-dvh place-items-center px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-8 text-center">
          <div className="mx-auto mb-4 grid size-12 place-items-center rounded-xl bg-brand text-xl font-semibold text-brand-ink">
            夕
          </div>
          <h1 className="text-2xl font-semibold">小夕客服控制台</h1>
          <p className="mt-2 text-sm text-muted">电商智能客服 · 流式对话 · 人工审批</p>
        </div>

        <div className="rounded-2xl border border-line bg-surface p-6 shadow-sm">
          {config.data?.demo_login && (
            <section className="mb-6">
              <h2 className="text-sm font-medium">演示账号</h2>
              <p className="mt-1 text-xs text-muted">以「小明」的身份体验，无需配置凭证。</p>
              <div className="mt-3 grid grid-cols-2 gap-3">
                <Button onClick={() => demo("customer")} disabled={!!busy}>
                  {busy === "customer" ? "登录中…" : "我是顾客"}
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => demo("approver")}
                  disabled={!!busy || !config.data.demo_approver_ready}
                  title={config.data.demo_approver_ready ? undefined : "需把 demo-reviewer 加入 REFUND_APPROVER_IDS"}
                >
                  {busy === "approver" ? "登录中…" : "我是审核员"}
                </Button>
              </div>
            </section>
          )}

          <form onSubmit={submit}>
            <label className="block text-sm font-medium" htmlFor="token">
              访问令牌
            </label>
            <p className="mt-1 text-xs text-muted">由服务端 AUTH_TOKENS 配置签发的 Bearer token。</p>
            <input
              id="token"
              type="password"
              autoComplete="off"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              className="mt-3 w-full rounded-lg border border-line bg-surface px-3 py-2.5 text-sm outline-none focus:border-brand"
              placeholder="粘贴 token"
            />
            <Button type="submit" variant="secondary" className="mt-3 w-full" disabled={!token.trim() || !!busy}>
              {busy === "token" ? "验证中…" : "使用令牌登录"}
            </Button>
          </form>

          <p role="alert" className="mt-4 min-h-5 text-sm text-danger">
            {error}
          </p>
        </div>
      </div>
    </main>
  );
}
