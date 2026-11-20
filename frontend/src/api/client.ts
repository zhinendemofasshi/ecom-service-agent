import type {
  AgentResponse,
  ApprovalAction,
  ApprovalDecision,
  AuthConfig,
  Identity,
  LoginResult,
  SessionState,
  SessionSummary,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type TokenGetter = () => string | null;
let getToken: TokenGetter = () => null;
let onUnauthorized: () => void = () => {};

/** Wired once by AuthProvider so every request carries the current token. */
export function configureClient(opts: { getToken: TokenGetter; onUnauthorized: () => void }) {
  getToken = opts.getToken;
  onUnauthorized = opts.onUnauthorized;
}

export function authHeaders(token = getToken()): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function detailOf(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    return typeof detail === "string" ? detail : JSON.stringify(detail);
  }
  return fallback;
}

export async function request<T>(
  path: string,
  init: RequestInit & { token?: string | null } = {},
): Promise<T> {
  const { token, headers, ...rest } = init;
  const response = await fetch(path, {
    ...rest,
    headers: {
      ...(rest.body ? { "Content-Type": "application/json" } : {}),
      ...authHeaders(token === undefined ? getToken() : token),
      ...headers,
    },
  });
  if (response.status === 204) return undefined as T;
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401 && token === undefined) onUnauthorized();
    throw new ApiError(response.status, detailOf(body, `请求失败（${response.status}）`));
  }
  return body as T;
}

const json = (value: unknown) => JSON.stringify(value);
const sid = (id: string) => encodeURIComponent(id);

export const api = {
  authConfig: () => request<AuthConfig>("/auth/config", { token: null }),
  demoLogin: (role: "customer" | "approver") =>
    request<LoginResult>("/auth/demo-login", { method: "POST", body: json({ role }), token: null }),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  me: (token?: string) => request<Identity>("/me", { token }),

  listSessions: () => request<{ items: SessionSummary[] }>("/sessions").then((r) => r.items),
  createSession: () => request<{ session_id: string }>("/sessions", { method: "POST" }),
  sessionState: (id: string) => request<SessionState>(`/sessions/${sid(id)}`),

  sendMessage: (id: string, text: string, requestId: string) =>
    request<{ request_id: string; pending_approval: boolean; response: AgentResponse }>(`/sessions/${sid(id)}/messages`, {
      method: "POST",
      body: json({ text }),
      headers: { "X-Request-ID": requestId },
    }),
  cancelRun: (requestId: string) =>
    request<{ status: string }>(`/runs/${sid(requestId)}/cancel`, { method: "POST" }),

  pendingApprovals: (id: string) =>
    request<{ actions: ApprovalAction[]; server_time: number }>(`/sessions/${sid(id)}/approvals`),
  decide: (id: string, decisions: ApprovalDecision[]) =>
    request<{ pending_approval: boolean }>(`/sessions/${sid(id)}/approval`, {
      method: "POST",
      body: json({ decisions }),
    }),
};
