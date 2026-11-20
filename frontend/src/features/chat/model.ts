import type { AgentEvent, AgentResponse, ApprovalAction, StoredMessage } from "@/api/types";

// ---------------------------------------------------------------------------
// Settled history (from the LangGraph checkpoint)
// ---------------------------------------------------------------------------

export interface ToolTurn {
  kind: "tool";
  id: string;
  name: string;
  args: Record<string, unknown>;
  /** Parsed tool output; undefined while the tool is still running. */
  output?: unknown;
  status: "running" | "done" | "failed";
  error?: string;
}

export type Turn =
  | { kind: "user"; id: string; text: string }
  | ToolTurn
  | { kind: "assistant"; id: string; text: string; response?: AgentResponse };

function parseJSON(value: string | null | undefined): unknown {
  if (!value) return undefined;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function isFailure(output: unknown): boolean {
  return !!output && typeof output === "object" && (output as { success?: unknown }).success === false;
}

/** Convert stored chat-format messages into renderable turns. */
export function historyToTurns(messages: StoredMessage[], lastResponse?: AgentResponse | null): Turn[] {
  const turns: Turn[] = [];
  const tools = new Map<string, ToolTurn>();
  messages.forEach((m, i) => {
    if (m.role === "user") turns.push({ kind: "user", id: `u${i}`, text: m.content });
    else if (m.role === "assistant") {
      for (const call of m.tool_calls ?? []) {
        const args = parseJSON(call.function.arguments);
        const turn: ToolTurn = {
          kind: "tool",
          id: call.id,
          name: call.function.name,
          args: args && typeof args === "object" ? (args as Record<string, unknown>) : {},
          status: "running",
        };
        tools.set(call.id, turn);
        turns.push(turn);
      }
      if (m.content) turns.push({ kind: "assistant", id: `a${i}`, text: m.content });
    } else if (m.role === "tool") {
      const turn = tools.get(m.tool_call_id);
      if (turn) {
        turn.output = parseJSON(m.content);
        turn.status = isFailure(turn.output) ? "failed" : "done";
      }
    }
  });
  // Tool calls with no stored output (e.g. interrupted before execution).
  for (const t of tools.values()) if (t.output === undefined) t.status = "done";

  if (lastResponse) {
    const last = [...turns].reverse().find((t) => t.kind === "assistant");
    if (last && last.kind === "assistant" && last.text === lastResponse.reply) last.response = lastResponse;
  }
  return turns;
}

// ---------------------------------------------------------------------------
// Live run (driven by SSE events while the agent works)
// ---------------------------------------------------------------------------

export type RunStatus = "thinking" | "streaming" | "tool" | "awaiting_approval" | "done" | "failed" | "cancelled";

export interface LiveRun {
  requestId: string;
  /** null when the run was adopted from the event stream (e.g. resumed after approval elsewhere). */
  userText: string | null;
  status: RunStatus;
  text: string;
  tools: Array<{ name: string; status: "running" | "done" | "failed"; attempt: number; error?: string }>;
  agent?: string;
  approvals?: ApprovalAction[];
  handoff?: string[];
  response?: AgentResponse;
  error?: string;
}

export const TERMINAL: ReadonlySet<RunStatus> = new Set(["done", "failed", "cancelled"]);

export function startRun(requestId: string, userText: string | null): LiveRun {
  return { requestId, userText, status: "thinking", text: "", tools: [] };
}

export function applyEvent(run: LiveRun, event: AgentEvent): LiveRun {
  switch (event.type) {
    case "model_started":
      return run.status === "awaiting_approval" ? { ...run, status: "thinking" } : run;
    case "token_delta":
      return { ...run, status: "streaming", text: run.text + event.delta };
    case "model_completed":
      // Text streamed before a tool call is scratch work; the final answer comes later.
      return event.tool_calls.length ? { ...run, text: "" } : run;
    case "tool_started": {
      const tools = [...run.tools];
      const last = tools[tools.length - 1];
      if (event.attempt > 1 && last?.name === event.name) tools[tools.length - 1] = { name: last.name, status: "running", attempt: event.attempt };
      else tools.push({ name: event.name, status: "running", attempt: event.attempt });
      return { ...run, status: "tool", tools, approvals: undefined };
    }
    case "tool_completed":
    case "tool_failed": {
      const tools = [...run.tools];
      const index = tools.map((t) => t.name).lastIndexOf(event.name);
      if (index >= 0) {
        tools[index] =
          event.type === "tool_completed"
            ? { ...tools[index], status: "done", error: undefined }
            : { ...tools[index], status: "failed", error: event.error };
      }
      return { ...run, tools };
    }
    case "agent_routed":
    case "agent_started":
      return { ...run, agent: event.agent };
    case "approval_required":
      return { ...run, status: "awaiting_approval", approvals: event.approval.actions };
    case "handoff_required":
      return { ...run, handoff: event.reasons };
    case "run_completed":
      return { ...run, status: "done", response: event.response, approvals: undefined };
    case "run_failed":
      return { ...run, status: "failed", error: event.error };
    case "run_cancelled":
    case "run_expired":
    case "approval_expired":
      return { ...run, status: "cancelled", approvals: undefined };
    default:
      return run;
  }
}

export const TOOL_LABELS: Record<string, string> = {
  query_order: "查询订单",
  query_logistics: "查询物流",
  query_product: "搜索商品",
  list_user_orders: "获取订单列表",
  search_knowledge: "检索知识库",
  apply_refund: "提交退款",
  recall_user_memory: "回忆用户偏好",
  load_skill: "加载技能",
};

export const toolLabel = (name: string) => TOOL_LABELS[name] ?? name;

export const INTENT_LABELS: Record<string, string> = {
  order_query: "订单查询",
  return_request: "退换货",
  product_consult: "商品咨询",
  complaint: "投诉",
  after_sale: "售后",
  promotion: "优惠活动",
  account: "账户",
  greeting: "问候",
  other: "其他",
};

export const AGENT_LABELS: Record<string, string> = {
  presale: "售前助手",
  postsale: "售后助手",
  complaint: "投诉专员",
};
