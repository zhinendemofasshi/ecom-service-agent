import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { NavLink, useLocation, useNavigate, useParams } from "react-router-dom";
import { api } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import { Badge, Button } from "@/components/ui";
import { cn } from "@/lib/cn";
import { Composer } from "./Composer";
import { ApprovalBanner, LiveRunView, TurnView } from "./Messages";
import { useChatSession } from "./useChatSession";

const SUGGESTIONS = ["我的订单还没到，帮我查一下物流", "有没有适合跑步的运动鞋推荐？", "鞋子开胶了，我要退货退款", "退货运费谁承担？"];

function relativeTime(iso: string) {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return new Date(iso).toLocaleDateString();
}

export function SessionList({ basePath }: { basePath: "/chat" | "/approvals" }) {
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: api.listSessions, refetchInterval: 20_000 });
  if (sessions.isLoading) return <p className="px-3 py-2 text-sm text-muted">加载中…</p>;
  if (sessions.error) return <p className="px-3 py-2 text-sm text-danger">{(sessions.error as Error).message}</p>;
  if (!sessions.data?.length) return <p className="px-3 py-2 text-sm text-muted">还没有会话</p>;
  return (
    <ul className="space-y-0.5">
      {sessions.data.map((s) => (
        <li key={s.session_id}>
          <NavLink
            to={`${basePath}/${s.session_id}`}
            className={({ isActive }) =>
              cn("block rounded-lg px-3 py-2 transition", isActive ? "bg-brand-soft" : "hover:bg-sunken")
            }
          >
            <div className="flex items-center gap-2">
              <span className="truncate text-sm">{s.title}</span>
              {s.pending_approvals > 0 && (
                <Badge tone="warn" className="ml-auto shrink-0">
                  待审
                </Badge>
              )}
            </div>
            <p className="text-xs text-muted">{relativeTime(s.updated_at)}</p>
          </NavLink>
        </li>
      ))}
    </ul>
  );
}

function EmptyState({ onPick, disabled }: { onPick: (text: string) => void; disabled?: boolean }) {
  return (
    <div className="mx-auto max-w-xl py-16 text-center">
      <div className="mx-auto mb-4 grid size-12 place-items-center rounded-2xl bg-brand text-xl text-brand-ink">夕</div>
      <h2 className="text-xl font-semibold">你好，我是小夕</h2>
      <p className="mt-2 text-sm text-muted">可以帮你查订单和物流、推荐商品、办理退换货。</p>
      <div className="mt-6 grid gap-2 sm:grid-cols-2">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            disabled={disabled}
            onClick={() => onPick(s)}
            className="rounded-xl border border-line bg-surface px-4 py-3 text-left text-sm transition hover:border-brand disabled:opacity-50"
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

function useStickToBottom(dep: unknown) {
  const ref = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onScroll = () => {
      pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);
  useLayoutEffect(() => {
    if (pinned.current && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [dep]);
  return ref;
}

function ChatView({ sessionId }: { sessionId: string }) {
  const { identity } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const chat = useChatSession(sessionId);
  const scrollRef = useStickToBottom([chat.turns, chat.run]);

  // First message typed before the session existed (see NewChat).
  const draft = (location.state as { draft?: string } | null)?.draft;
  const sentDraft = useRef(false);
  useEffect(() => {
    if (draft && !chat.loading && !sentDraft.current) {
      sentDraft.current = true;
      navigate(location.pathname, { replace: true, state: null });
      void chat.send(draft);
    }
  }, [draft, chat, navigate, location.pathname]);

  const awaiting = chat.approvals.length > 0 && !chat.busy;
  const pendingRequest = chat.run?.status === "awaiting_approval" ? chat.run.requestId : undefined;

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col">
      <div className="flex h-10 shrink-0 items-center justify-end gap-2 px-4 text-xs text-muted">
        <span className={cn("size-1.5 rounded-full", chat.stream === "live" ? "bg-brand" : "bg-warn")} />
        {chat.stream === "live" ? "实时连接" : chat.stream === "offline" ? "连接已断开" : "正在连接…"}
      </div>
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-4">
        <div className="mx-auto max-w-3xl space-y-5 pb-6">
          {chat.loading && <p className="py-10 text-center text-sm text-muted">加载历史消息…</p>}
          {chat.error && <p className="py-10 text-center text-sm text-danger">{chat.error.message}</p>}
          {!chat.loading && !chat.error && !chat.turns.length && !chat.run && (
            <EmptyState onPick={(t) => void chat.send(t)} />
          )}
          {chat.turns.map((t) => (
            <TurnView key={t.id} turn={t} />
          ))}
          {chat.run && <LiveRunView run={chat.run} onDismiss={chat.dismissError} />}
          {awaiting && (
            <ApprovalBanner
              sessionId={sessionId}
              actions={chat.approvals}
              canApprove={!!identity?.can_approve}
              onWithdraw={pendingRequest ? () => void chat.cancel() : undefined}
            />
          )}
        </div>
      </div>
      <div className="shrink-0 px-4 pb-4">
        <div className="mx-auto max-w-3xl">
          <Composer
            busy={chat.busy}
            disabled={awaiting}
            placeholder={awaiting ? "请等待当前退款申请审核完成" : undefined}
            onSend={(t) => void chat.send(t)}
            onStop={() => void chat.cancel()}
          />
        </div>
      </div>
    </div>
  );
}

function NewChat() {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function start(text?: string) {
    setBusy(true);
    setError("");
    try {
      const { session_id } = await api.createSession();
      navigate(`/chat/${session_id}`, { state: text ? { draft: text } : null });
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }
  return (
    <div className="flex h-full min-w-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto px-4">
        <EmptyState onPick={(t) => void start(t)} disabled={busy} />
        {error && <p className="text-center text-sm text-danger">{error}</p>}
      </div>
      <div className="shrink-0 px-4 pb-4">
        <div className="mx-auto max-w-3xl">
          <Composer busy={false} disabled={busy} onSend={(t) => void start(t)} onStop={() => {}} />
        </div>
      </div>
    </div>
  );
}

export function ChatPage() {
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const [drawer, setDrawer] = useState(false);
  useEffect(() => setDrawer(false), [sessionId]);

  return (
    <div className="flex h-full">
      <aside
        className={cn(
          "w-64 shrink-0 flex-col border-r border-line bg-surface md:flex",
          drawer ? "fixed inset-y-14 left-0 z-20 flex shadow-xl" : "hidden",
        )}
      >
        <div className="p-3">
          <Button className="w-full" onClick={() => navigate("/chat")}>
            + 新对话
          </Button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
          <SessionList basePath="/chat" />
        </div>
      </aside>
      <div className="relative flex min-w-0 flex-1">
        <button
          className="absolute top-2 left-3 z-10 text-xs text-muted md:hidden"
          onClick={() => setDrawer((v) => !v)}
        >
          ☰ 会话
        </button>
        {sessionId ? <ChatView key={sessionId} sessionId={sessionId} /> : <NewChat />}
      </div>
    </div>
  );
}
