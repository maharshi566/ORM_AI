// Every call the website makes to the FastAPI backend. The browser only ever talks to
// the backend at NEXT_PUBLIC_API_URL; no AI or database keys live here. The login
// token (lib/login.ts) is sent as "Authorization: Bearer <token>".
// NEXT_PUBLIC_ values are fixed at build time, so set it before `npm run build`.

import { getLogin, setLogin, type Login } from "@/lib/login";
import { readSse } from "@/lib/sse";
import type {
  ApiErrorBody,
  ApprovalDecisionRequest,
  ApprovalListResponse,
  ApprovalStatusResponse,
  ChatResponse,
  DevUser,
  EvaluationsResponse,
  HealthResponse,
  MeResponse,
  MetricsResponse,
  SessionListItem,
  SessionResponse,
  ShopView,
  StageEvent,
  StreamErrorEvent,
  TokenResponse,
  WorkflowListItem,
  WorkflowResponse,
} from "@/types/api";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly retryAfter: number | null;

  constructor(message: string, status: number, code: string, requestId: string | null = null, retryAfter: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.retryAfter = retryAfter;
  }
}

const UNREACHABLE = `Could not reach the ORM_AI server at ${API_URL}. Check that the backend is running.`;

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429 && error.retryAfter) {
      return `${error.message} Try again in ${error.retryAfter} seconds.`;
    }
    return error.message;
  }
  return error instanceof Error ? error.message : "Something went wrong.";
}

async function toApiError(response: Response): Promise<ApiError> {
  const requestId = response.headers.get("X-Request-ID");
  const retry = Number(response.headers.get("Retry-After"));
  const retryAfter = Number.isFinite(retry) && retry > 0 ? retry : null;
  try {
    const body = (await response.json()) as ApiErrorBody;
    return new ApiError(body.error.message, response.status, body.error.code, requestId, retryAfter);
  } catch {
    return new ApiError(`The server answered with HTTP ${response.status}.`, response.status, "http_error", requestId, retryAfter);
  }
}

function headers(json: boolean, login: Login | null): HeadersInit {
  const result: Record<string, string> = {};
  if (json) result["Content-Type"] = "application/json";
  if (login) result.Authorization = `Bearer ${login.token}`;
  return result;
}

async function send(path: string, init: RequestInit & { json?: unknown } = {}): Promise<Response> {
  const login = getLogin();
  const { json, ...rest } = init;
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      cache: "no-store",
      ...rest,
      headers: { ...headers(json !== undefined, login), ...rest.headers },
      body: json !== undefined ? JSON.stringify(json) : rest.body,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(UNREACHABLE, 0, "unreachable");
  }
  if (!response.ok) {
    const error = await toApiError(response);
    // A token the server no longer accepts (expired, user switched off): log out here too.
    if (error.status === 401 && login) setLogin(null);
    throw error;
  }
  return response;
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return (await (await send(path, { signal })).json()) as T;
}

async function postJson<T>(path: string, json: unknown, signal?: AbortSignal): Promise<T> {
  return (await (await send(path, { method: "POST", json, signal })).json()) as T;
}

function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

// ------------------------------------------------------------------ health and login

/** GET /api/health. A 503 still carries a body saying which part is down. */
export async function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}/api/health`, { cache: "no-store", signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(UNREACHABLE, 0, "unreachable");
  }
  if (response.status !== 200 && response.status !== 503) throw await toApiError(response);
  return (await response.json()) as HealthResponse;
}

export const getDevUsers = (signal?: AbortSignal) => getJson<DevUser[]>("/api/auth/dev-users", signal);

export const getMe = (signal?: AbortSignal) => getJson<MeResponse>("/api/auth/me", signal);

/** POST /api/auth/dev-token, then remember the login in this browser. */
export async function devLogin(userId: string): Promise<Login> {
  const token = await postJson<TokenResponse>("/api/auth/dev-token", { user_id: userId });
  const login: Login = {
    token: token.access_token,
    userId: token.user_id,
    name: token.name,
    role: token.role,
    shopId: token.shop_id,
    shopName: token.shop_name,
    expiresAt: Date.now() + token.expires_in * 1000,
  };
  setLogin(login);
  return login;
}

/** Log in with a token from another login system: ask the server who it belongs to. */
export async function tokenLogin(token: string): Promise<Login> {
  const trimmed = token.trim().replace(/^Bearer\s+/i, "");
  let response: Response;
  try {
    response = await fetch(`${API_URL}/api/auth/me`, {
      cache: "no-store",
      headers: { Authorization: `Bearer ${trimmed}` },
    });
  } catch {
    throw new ApiError(UNREACHABLE, 0, "unreachable");
  }
  if (!response.ok) throw await toApiError(response);
  const me = (await response.json()) as MeResponse;
  if (!me.authenticated || !me.user_id || !me.role) {
    throw new ApiError("The server did not accept this token.", 401, "unauthorized");
  }
  const login: Login = {
    token: trimmed,
    userId: me.user_id,
    name: me.name,
    role: me.role,
    shopId: me.shop_id,
    shopName: me.shop_name,
    expiresAt: tokenExpiry(trimmed) ?? Date.now() + 12 * 3600 * 1000,
  };
  setLogin(login);
  return login;
}

function tokenExpiry(token: string): number | null {
  try {
    const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const exp = (JSON.parse(atob(payload)) as { exp?: number }).exp;
    return typeof exp === "number" ? exp * 1000 : null;
  } catch {
    return null;
  }
}

export function logout(): void {
  setLogin(null);
}

// ------------------------------------------------------------------ chat

export type ChatRequest = { shop_id: string; message: string; session_id?: string | null };

export type StreamHandlers = {
  onStage?: (event: StageEvent) => void;
  onApproval?: (approval: unknown) => void;
};

/**
 * POST /api/chat/stream: follows each agent as it starts and finishes, then returns
 * the same reply POST /api/chat gives. Throws ApiError for an error event.
 */
export async function chatStream(body: ChatRequest, handlers: StreamHandlers = {}, signal?: AbortSignal): Promise<ChatResponse> {
  const response = await send("/api/chat/stream", {
    method: "POST",
    json: body,
    signal,
    headers: { Accept: "text/event-stream" },
  });
  if (!response.body) throw new ApiError("The server sent no progress stream.", 0, "no_stream");
  let result: ChatResponse | null = null;
  let failure: ApiError | null = null;
  await readSse(response.body, (message) => {
    let data: unknown;
    try {
      data = JSON.parse(message.data);
    } catch {
      return; // not ours; ignore
    }
    if (message.event === "stage") handlers.onStage?.(data as StageEvent);
    else if (message.event === "approval_requested") handlers.onApproval?.(data);
    else if (message.event === "result") result = data as ChatResponse;
    else if (message.event === "error") {
      const error = data as StreamErrorEvent;
      failure = new ApiError(error.message, error.status, error.code);
      if (error.status === 401 && getLogin()) setLogin(null);
    }
  });
  if (failure) throw failure;
  if (!result) throw new ApiError("The connection closed before the answer arrived. Try again.", 0, "stream_cut");
  return result;
}

// ------------------------------------------------------------------ approvals

export const decideApproval = (workflowId: string, body: ApprovalDecisionRequest) =>
  postJson<ChatResponse>(`/api/approval/${encodeURIComponent(workflowId)}`, body);

export const getApproval = (workflowId: string, signal?: AbortSignal) =>
  getJson<ApprovalStatusResponse>(`/api/approval/${encodeURIComponent(workflowId)}`, signal);

export const listApprovals = (params: { status?: string; shop_id?: string; limit?: number }, signal?: AbortSignal) =>
  getJson<ApprovalListResponse>(`/api/approvals${query(params)}`, signal);

// ------------------------------------------------------------------ records

export const listShops = (signal?: AbortSignal) => getJson<{ shops: ShopView[] }>("/api/shops", signal);

export const listSessions = (params: { limit?: number; shop_id?: string } = {}, signal?: AbortSignal) =>
  getJson<{ sessions: SessionListItem[] }>(`/api/sessions${query(params)}`, signal);

export const getSession = (sessionId: string, signal?: AbortSignal) =>
  getJson<SessionResponse>(`/api/sessions/${encodeURIComponent(sessionId)}`, signal);

export const listWorkflows = (params: { status?: string; shop_id?: string; limit?: number } = {}, signal?: AbortSignal) =>
  getJson<{ workflows: WorkflowListItem[] }>(`/api/workflows${query(params)}`, signal);

export const getWorkflow = (workflowId: string, signal?: AbortSignal) =>
  getJson<WorkflowResponse>(`/api/workflows/${encodeURIComponent(workflowId)}`, signal);

export const getMetrics = (signal?: AbortSignal) => getJson<MetricsResponse>("/api/metrics", signal);

export const getEvaluations = (signal?: AbortSignal) => getJson<EvaluationsResponse>("/api/evaluations", signal);
