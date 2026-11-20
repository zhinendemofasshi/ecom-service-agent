import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { ApiError, api } from "@/api/client";
import { subscribeEvents } from "@/api/sse";
import type { AgentEvent, ApprovalAction, ApprovalDecision } from "@/api/types";
import { useAuth } from "@/auth/AuthContext";
import { Badge, Button, Spinner } from "@/components/ui";
import { cn } from "@/lib/cn";
import { SessionList } from "@/features/chat/ChatPage";
import { historyToTurns } from "@/features/chat/model";

type Draft = { type?: ApprovalDecision["type"]; reason: string; note: string };

const NOTIFY: Partial<Record<AgentEvent["type"], string>> = {
  approval_required: "有新的待审核操作",
  approval_expired: "审批已超时，未执行退款",
  run_completed: "Agent 已完成回复",
  run_failed: "运行失败，请核对结果",
  run_cancelled: "用户已撤回申请",
  handoff_required: "已转人工",
};

/** Markdown → one-line plain text for the compact context panel. */
const plain = (md: string) =>
  md
    .replace(/[*_`#>]+/g, "")
    .replace(/^\s*(?:[-+]|\d+\.)\s+/gm, "")
    .replace(/\s+/g, " ")
    .trim();

function useNow(intervalMs = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

function countdown(ms: number) {
  if (ms <= 0) return "已过期";
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return h ? `${h} 小时 ${m} 分` : `${m}:${String(s).padStart(2, "0")}`;
}

function ActionCard({
  action,
  draft,
  onChange,
  disabled,
  now,
}: {
  action: ApprovalAction;
  draft: Draft;
  onChange: (d: Draft) => void;
  disabled: boolean;
  now: number;
}) {
  const remaining = action.expires_at * 1000 - now;
  const expired = remaining <= 0;
  const edited = draft.reason.trim() !== (action.reason ?? "");
  const choices: Array<[ApprovalDecision["type"], string]> = [
    ["approve", "批准"],
    ["edit", "修改后批准"],
    ["reject", "拒绝"],
  ];

  return (
    <article className={cn("rounded-2xl border bg-surface p-5", draft.type ? "border-brand/40" : "border-line")}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs text-muted">退款申请 · {action.name}</p>
          <p className="mt-1 font-mono text-sm">{action.order_id ?? "—"}</p>
        </div>
        <div className="text-right">
          <p className="text-2xl font-semibold tabular-nums">
            {action.amount == null ? "金额待核验" : `¥${action.amount.toFixed(2)}`}
          </p>
          <p className={cn("text-xs", remaining < 600_000 ? "text-danger" : "text-muted")}>剩余 {countdown(remaining)}</p>
        </div>
      </div>

      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <label className="text-sm">
          <span className="text-xs text-muted">退款原因 {edited && <Badge tone="warn">已修改</Badge>}</span>
          <input
            value={draft.reason}
            disabled={disabled || expired}
            onChange={(e) => onChange({ ...draft, reason: e.target.value, type: draft.type === "approve" ? "edit" : draft.type })}
            className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 outline-none focus:border-brand"
          />
          {edited && <span className="mt-1 block text-xs text-muted line-through">{action.reason}</span>}
        </label>
        <label className="text-sm">
          <span className="text-xs text-muted">审核说明（拒绝时必填）</span>
          <textarea
            value={draft.note}
            disabled={disabled || expired}
            onChange={(e) => onChange({ ...draft, note: e.target.value })}
            rows={2}
            className="mt-1 w-full resize-y rounded-lg border border-line bg-surface px-3 py-2 outline-none focus:border-brand"
          />
        </label>
      </div>

      <div className="mt-4 flex flex-wrap gap-2" role="radiogroup" aria-label="审批决定">
        {choices.map(([type, label]) => (
          <Button
            key={type}
            role="radio"
            aria-checked={draft.type === type}
            disabled={disabled || expired || (type === "edit" && !edited)}
            variant={draft.type === type ? (type === "reject" ? "danger" : "primary") : "ghost"}
            className={cn(draft.type !== type && "border border-line")}
            onClick={() => onChange({ ...draft, type })}
          >
            {label}
          </Button>
        ))}
      </div>
      <p className="mt-3 font-mono text-xs break-all text-muted">call_id: {action.call_id}</p>
    </article>
  );
}

function SessionApprovals({ sessionId }: { sessionId: string }) {
  const queryClient = useQueryClient();
  const now = useNow();
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [log, setLog] = useState<Array<{ at: string; text: string }>>([]);
  const [notice, setNotice] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  const approvals = useQuery({
    queryKey: ["approvals", sessionId],
    queryFn: () => api.pendingApprovals(sessionId),
    refetchInterval: 15_000,
  });
  const history = useQuery({ queryKey: ["session", sessionId], queryFn: () => api.sessionState(sessionId) });
  const actions = useMemo(() => approvals.data?.actions ?? [], [approvals.data]);

  // Live notifications for this session.
  const cursor = history.data?.event_cursor;
  useEffect(() => {
    if (cursor === undefined) return;
    const controller = new AbortController();
    void subscribeEvents({
      sessionId,
      after: cursor,
      signal: controller.signal,
      onResync: async () => {
        await queryClient.refetchQueries({ queryKey: ["session", sessionId] });
        return queryClient.getQueryData<{ event_cursor: number }>(["session", sessionId])?.event_cursor ?? 0;
      },
      onEvent: (event) => {
        const text = NOTIFY[event.type];
        if (!text) return;
        setLog((l) => [{ at: new Date(event.timestamp).toLocaleTimeString(), text }, ...l].slice(0, 30));
        void queryClient.invalidateQueries({ queryKey: ["approvals", sessionId] });
        void queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
        void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      },
    }).catch(() => undefined);
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- subscribe once per session
  }, [sessionId, cursor === undefined]);

  // Keep drafts for actions still pending; seed new ones from the request.
  useEffect(() => {
    setDrafts((prev) =>
      Object.fromEntries(actions.map((a) => [a.call_id, prev[a.call_id] ?? { reason: a.reason ?? "", note: "" }])),
    );
  }, [actions]);

  const submit = useMutation({
    mutationFn: (decisions: ApprovalDecision[]) => api.decide(sessionId, decisions),
    onSuccess: (result) => {
      setNotice({ tone: "ok", text: result.pending_approval ? "已提交，Agent 产生了新的待审核操作。" : "审核决定已执行，Agent 已回复用户。" });
    },
    onError: (error) => {
      const conflict = error instanceof ApiError && error.status === 409;
      setNotice({ tone: "error", text: conflict ? `该申请已被处理或已过期：${error.message}` : (error as Error).message });
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["approvals", sessionId] });
      void queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
    },
  });

  const ready = actions.length > 0 && actions.every((a) => drafts[a.call_id]?.type);
  const missingNote = actions.some((a) => drafts[a.call_id]?.type === "reject" && !drafts[a.call_id]?.note.trim());

  function decisions(): ApprovalDecision[] {
    return actions.map((a) => {
      const d = drafts[a.call_id];
      const decision: ApprovalDecision = { call_id: a.call_id, type: d.type!, message: d.note.trim() || undefined };
      if (d.type === "edit") decision.arguments = { ...a.arguments, order_id: a.order_id, reason: d.reason.trim() };
      return decision;
    });
  }

  const context = useMemo(
    () =>
      history.data
        ? historyToTurns(history.data.messages)
            .filter((t) => t.kind !== "tool")
            .slice(-6)
        : [],
    [history.data],
  );

  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_280px]">
      <section className="min-w-0 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">待审核操作</h2>
          <Badge tone={actions.length ? "warn" : "neutral"}>{actions.length} 项</Badge>
        </div>
        {notice && (
          <p className={cn("rounded-lg px-3 py-2 text-sm", notice.tone === "ok" ? "bg-brand-soft text-brand" : "bg-danger-soft text-danger")}>
            {notice.text}
          </p>
        )}
        {approvals.isLoading && <p className="text-sm text-muted">加载中…</p>}
        {approvals.error && <p className="text-sm text-danger">{(approvals.error as Error).message}</p>}
        {!approvals.isLoading && !actions.length && (
          <div className="rounded-2xl border border-dashed border-line py-14 text-center text-sm text-muted">
            这个会话当前没有待审批操作
          </div>
        )}
        {actions.map((a) => (
          <ActionCard
            key={a.call_id}
            action={a}
            now={now}
            disabled={submit.isPending}
            draft={drafts[a.call_id] ?? { reason: a.reason ?? "", note: "" }}
            onChange={(d) => setDrafts((prev) => ({ ...prev, [a.call_id]: d }))}
          />
        ))}
        {actions.length > 0 && (
          <div className="sticky bottom-0 flex flex-wrap items-center gap-3 rounded-2xl border border-line bg-surface p-4 shadow-sm">
            <p className="text-sm text-muted">
              {submit.isPending
                ? "已提交，Agent 正在继续执行…"
                : missingNote
                  ? "拒绝时请填写审核说明"
                  : ready
                    ? "同一批次的决定会一起执行"
                    : `还有 ${actions.filter((a) => !drafts[a.call_id]?.type).length} 项未做决定`}
            </p>
            <Button className="ml-auto" disabled={!ready || missingNote || submit.isPending} onClick={() => submit.mutate(decisions())}>
              {submit.isPending && <Spinner />}提交 {actions.length} 项决定
            </Button>
          </div>
        )}
      </section>

      <aside className="space-y-6">
        <section>
          <h3 className="mb-2 text-sm font-medium">对话上下文</h3>
          <div className="space-y-2 rounded-2xl border border-line bg-surface p-3 text-sm">
            {context.length === 0 && <p className="text-muted">暂无</p>}
            {context.map((t) => (
              <p key={t.id} className="line-clamp-3">
                <span className="text-muted">{t.kind === "user" ? "顾客：" : "小夕："}</span>
                {"text" in t ? plain(t.text) : ""}
              </p>
            ))}
          </div>
        </section>
        <section>
          <h3 className="mb-2 text-sm font-medium">实时通知</h3>
          <ul className="space-y-1 text-sm">
            {log.length === 0 && <li className="text-muted">等待事件…</li>}
            {log.map((l, i) => (
              <li key={i} className="flex gap-2">
                <span className="shrink-0 text-xs tabular-nums text-muted">{l.at}</span>
                {l.text}
              </li>
            ))}
          </ul>
        </section>
      </aside>
    </div>
  );
}

export function ApprovalsPage() {
  const { sessionId } = useParams();
  const { identity } = useAuth();
  return (
    <div className="flex h-full">
      <aside className="hidden w-64 shrink-0 flex-col border-r border-line bg-surface md:flex">
        <p className="px-4 pt-4 pb-2 text-xs font-medium text-muted">会话</p>
        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
          <SessionList basePath="/approvals" />
        </div>
      </aside>
      <main className="min-w-0 flex-1 overflow-y-auto px-4 py-6 md:px-8">
        <div className="mx-auto max-w-5xl">
          {!identity?.can_approve && (
            <p className="mb-4 rounded-lg bg-warn-soft px-3 py-2 text-sm text-warn">
              当前账号没有审批权限，只能查看和拒绝。请使用审核员账号批准退款。
            </p>
          )}
          {sessionId ? (
            <SessionApprovals key={sessionId} sessionId={sessionId} />
          ) : (
            <div className="py-20 text-center text-sm text-muted">从左侧选择带「待审」标记的会话</div>
          )}
        </div>
      </main>
    </div>
  );
}
