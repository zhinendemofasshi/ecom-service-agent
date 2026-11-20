import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/api/client";
import { subscribeEvents } from "@/api/sse";
import type { AgentEvent } from "@/api/types";
import { TERMINAL, applyEvent, historyToTurns, startRun } from "./model";
import type { LiveRun } from "./model";

export type StreamStatus = "connecting" | "live" | "reconnecting" | "offline";

/**
 * One chat session: settled history from the checkpoint plus a live run fed
 * by the session's SSE stream. When a run reaches a terminal state the history
 * is refetched and the live run is dropped in the same render, so the
 * streamed text is replaced by the persisted (guardrail-checked) reply without
 * flicker. Mount with key={sessionId} so switching sessions starts clean.
 */
export function useChatSession(sessionId: string | undefined) {
  const queryClient = useQueryClient();
  const [run, setRun] = useState<LiveRun | null>(null);
  const [stream, setStream] = useState<StreamStatus>("connecting");
  const runRef = useRef<LiveRun | null>(null);
  runRef.current = run;

  const history = useQuery({
    queryKey: ["session", sessionId],
    queryFn: () => api.sessionState(sessionId!),
    enabled: !!sessionId,
  });
  const pending = useQuery({
    queryKey: ["approvals", sessionId],
    queryFn: () => api.pendingApprovals(sessionId!),
    enabled: !!sessionId,
  });

  const settle = useCallback(async () => {
    await Promise.all([
      queryClient.refetchQueries({ queryKey: ["session", sessionId] }),
      queryClient.refetchQueries({ queryKey: ["approvals", sessionId] }),
    ]);
    void queryClient.invalidateQueries({ queryKey: ["sessions"] });
  }, [queryClient, sessionId]);

  const onEvent = useCallback(
    (event: AgentEvent) => {
      const current = runRef.current;
      let next: LiveRun | null = current;
      if (current && event.request_id === current.requestId) next = applyEvent(current, event);
      else if (!current || TERMINAL.has(current.status)) {
        // A run we didn't start in this tab, e.g. resumed by an approver.
        if (event.type === "run_started" || event.type === "model_started" || event.type === "tool_started") {
          next = applyEvent(startRun(event.request_id, null), event);
        }
      }
      if (next === current) return;
      runRef.current = next;
      setRun(next);
      if (next && next.status === "awaiting_approval") {
        // History now holds the user's message; stop echoing it from the live run.
        void settle().then(() => {
          const r = runRef.current;
          if (r?.requestId === next!.requestId && r.userText) {
            runRef.current = { ...r, userText: null };
            setRun(runRef.current);
          }
        });
      }
      if (next && TERMINAL.has(next.status) && next.status !== "failed") {
        void settle().then(() => {
          if (runRef.current?.requestId === next!.requestId) {
            runRef.current = null;
            setRun(null);
          }
        });
      }
    },
    [settle],
  );

  // Subscribe once per session, starting at the cursor from the first history load.
  const cursor = history.data?.event_cursor;
  const hasHistory = cursor !== undefined;
  useEffect(() => {
    if (!sessionId || !hasHistory) return;
    const controller = new AbortController();
    setStream("connecting");
    subscribeEvents({
      sessionId,
      after: queryClient.getQueryData<{ event_cursor: number }>(["session", sessionId])?.event_cursor ?? 0,
      signal: controller.signal,
      onEvent,
      onStatus: setStream,
      onResync: async () => {
        await settle();
        return queryClient.getQueryData<{ event_cursor: number }>(["session", sessionId])?.event_cursor ?? 0;
      },
    }).catch(() => setStream("offline"));
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- cursor read once at subscribe time
  }, [sessionId, hasHistory]);

  const send = useCallback(
    async (text: string, targetSession = sessionId) => {
      if (!targetSession) return;
      const requestId = crypto.randomUUID();
      const fresh = startRun(requestId, text);
      runRef.current = fresh;
      setRun(fresh);
      try {
        const result = await api.sendMessage(targetSession, text, requestId);
        if (!result.pending_approval && runRef.current?.requestId === requestId && !TERMINAL.has(runRef.current.status)) {
          // Events normally settle the run; this covers a stream that missed run_completed.
          onEvent({ type: "run_completed", request_id: requestId, sequence: 0, timestamp: "", response: result.response });
        }
      } catch (error) {
        if (runRef.current?.requestId !== requestId) return;
        if (error instanceof ApiError) {
          const cancelled = error.status === 409 && error.message.includes("取消");
          const next: LiveRun = { ...runRef.current, status: cancelled ? "cancelled" : "failed", error: error.message };
          runRef.current = next;
          setRun(next);
          if (cancelled) void settle().then(() => setRun((r) => (r?.requestId === requestId ? null : r)));
        }
        // Network errors: the server keeps running in the background and the
        // event stream will deliver the outcome, so leave the run as is.
      }
    },
    [sessionId, onEvent, settle],
  );

  const cancel = useCallback(async () => {
    const current = runRef.current;
    if (current) await api.cancelRun(current.requestId).catch(() => undefined);
  }, []);

  const dismissError = useCallback(() => {
    runRef.current = null;
    setRun(null);
  }, []);

  const turns = useMemo(
    () => (history.data ? historyToTurns(history.data.messages, history.data.response) : []),
    [history.data],
  );

  const approvals = run?.approvals ?? pending.data?.actions ?? [];
  const busy = !!run && !TERMINAL.has(run.status) && run.status !== "awaiting_approval";

  return {
    turns,
    run,
    approvals,
    busy,
    stream,
    loading: history.isLoading,
    error: history.error as Error | null,
    send,
    cancel,
    dismissError,
  };
}
