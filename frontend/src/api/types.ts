// Shapes mirrored from app/api.py, app/schemas/response.py and the LangGraph
// runtime's event emitter. Keep in sync when the backend contract changes.

export interface Identity {
  tenant_id: string;
  user_id: string;
  actor_id: string;
  is_admin: boolean;
  can_approve: boolean;
}

export interface AuthConfig {
  demo_login: boolean;
  demo_approver_ready: boolean;
}

export interface LoginResult {
  token: string;
  expires_at: number;
  identity: Identity;
}

export interface SessionSummary {
  session_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  pending_approvals: number;
}

export type Intent =
  | "order_query"
  | "return_request"
  | "product_consult"
  | "complaint"
  | "after_sale"
  | "promotion"
  | "account"
  | "greeting"
  | "other";

export interface AgentResponse {
  intent: Intent;
  confidence: number;
  reply: string;
  requires_human: boolean;
  follow_up_question: string | null;
}

/** Messages stored in the LangGraph checkpoint (OpenAI chat format, schema v2). */
export type StoredMessage =
  | { role: "user"; content: string }
  | { role: "assistant"; content: string | null; tool_calls?: StoredToolCall[] }
  | { role: "tool"; tool_call_id: string; content: string }
  | { role: "system"; content: string };

export interface StoredToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

export interface SessionState {
  messages: StoredMessage[];
  summary: string | null;
  response: AgentResponse | null;
  /** Latest event sequence at read time; subscribe with after=event_cursor. */
  event_cursor: number;
}

export interface ApprovalAction {
  call_id: string;
  name: string;
  arguments: Record<string, unknown>;
  order_id: string | null;
  reason: string | null;
  amount: number | null;
  expires_at: number;
}

export interface ApprovalDecision {
  call_id: string;
  type: "approve" | "edit" | "reject";
  arguments?: Record<string, unknown>;
  message?: string;
}

interface BaseEvent {
  sequence: number;
  timestamp: string;
  request_id: string;
}

export type AgentEvent = BaseEvent &
  (
    | { type: "run_started" }
    | { type: "model_started"; step?: number }
    | { type: "token_delta"; delta: string }
    | { type: "model_completed"; tool_calls: string[] }
    | { type: "tool_started"; name: string; attempt: number }
    | { type: "tool_completed"; name: string; attempt?: number }
    | { type: "tool_failed"; name: string; attempt: number; error: string }
    | { type: "agent_routed"; agent: string; source?: string }
    | { type: "agent_started"; agent: string }
    | { type: "approval_required"; approval: { actions: ApprovalAction[] } }
    | { type: "approval_expired"; message?: string }
    | { type: "handoff_required"; reasons: string[] }
    | { type: "guardrail_blocked"; issues: string[] }
    | { type: "context_compacted"; removed_messages: number }
    | { type: "run_completed"; response: AgentResponse }
    | { type: "run_failed"; error: string }
    | { type: "run_cancelled" }
    | { type: "run_expired" }
  );

export type AgentEventType = AgentEvent["type"];
