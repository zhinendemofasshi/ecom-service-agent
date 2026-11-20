import type { AgentEvent, StoredMessage } from "@/api/types";
import { applyEvent, historyToTurns, startRun } from "./model";

const ev = (e: Record<string, unknown>) => ({ sequence: 1, timestamp: "", request_id: "r1", ...e }) as AgentEvent;

describe("historyToTurns", () => {
  it("pairs tool calls with their outputs and attaches final metadata", () => {
    const messages: StoredMessage[] = [
      { role: "user", content: "查订单" },
      {
        role: "assistant",
        content: null,
        tool_calls: [{ id: "c1", type: "function", function: { name: "query_order", arguments: '{"order_id":"O1"}' } }],
      },
      { role: "tool", tool_call_id: "c1", content: '{"success":true,"order":{"order_id":"O1"}}' },
      {
        role: "assistant",
        content: null,
        tool_calls: [{ id: "c2", type: "function", function: { name: "query_logistics", arguments: "{}" } }],
      },
      { role: "tool", tool_call_id: "c2", content: '{"success":false,"error":"boom"}' },
      { role: "assistant", content: "已发货" },
    ];
    const response = { intent: "order_query", confidence: 0.9, reply: "已发货", requires_human: false, follow_up_question: null } as const;
    const turns = historyToTurns(messages, response);

    expect(turns.map((t) => t.kind)).toEqual(["user", "tool", "tool", "assistant"]);
    expect(turns[1]).toMatchObject({ name: "query_order", args: { order_id: "O1" }, status: "done" });
    expect(turns[2]).toMatchObject({ status: "failed" });
    expect(turns[3]).toMatchObject({ response });
  });
});

describe("applyEvent", () => {
  it("streams tokens, drops pre-tool scratch text and tracks retries", () => {
    let run = startRun("r1", "hi");
    run = applyEvent(run, ev({ type: "token_delta", delta: "让我查" }));
    run = applyEvent(run, ev({ type: "model_completed", tool_calls: ["query_order"] }));
    expect(run.text).toBe("");

    run = applyEvent(run, ev({ type: "tool_started", name: "query_order", attempt: 1 }));
    run = applyEvent(run, ev({ type: "tool_failed", name: "query_order", attempt: 1, error: "timeout" }));
    run = applyEvent(run, ev({ type: "tool_started", name: "query_order", attempt: 2 }));
    expect(run.tools).toEqual([{ name: "query_order", status: "running", attempt: 2 }]);
    run = applyEvent(run, ev({ type: "tool_completed", name: "query_order" }));
    expect(run.tools[0].status).toBe("done");

    run = applyEvent(run, ev({ type: "token_delta", delta: "已" }));
    run = applyEvent(run, ev({ type: "token_delta", delta: "发货" }));
    expect(run).toMatchObject({ status: "streaming", text: "已发货" });
  });

  it("moves through approval to completion", () => {
    let run = startRun("r1", "退款");
    const action = { call_id: "c1", name: "apply_refund", arguments: {}, order_id: "O1", reason: "坏了", amount: 10, expires_at: 0 };
    run = applyEvent(run, ev({ type: "approval_required", approval: { actions: [action] } }));
    expect(run.status).toBe("awaiting_approval");
    run = applyEvent(run, ev({ type: "model_started" }));
    expect(run.status).toBe("thinking");
    run = applyEvent(run, ev({ type: "run_completed", response: { reply: "ok" } }));
    expect(run).toMatchObject({ status: "done", approvals: undefined });
  });
});
