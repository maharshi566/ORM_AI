import type { ApiErrorBody, HealthResponse } from "@/types/api";

// The browser only ever talks to the FastAPI backend. No LLM or database keys live here.
// NEXT_PUBLIC_ values are inlined at build time, so set this before `npm run build`.
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(
  /\/+$/,
  "",
);

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;

  constructor(message: string, status: number, code: string, requestId: string | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

async function toApiError(response: Response): Promise<ApiError> {
  const requestId = response.headers.get("X-Request-ID");
  try {
    const body = (await response.json()) as ApiErrorBody;
    return new ApiError(body.error.message, response.status, body.error.code, requestId);
  } catch {
    return new ApiError(`Request failed with HTTP ${response.status}`, response.status, "http_error", requestId);
  }
}

/** Generic JSON request helper for the endpoints added in later phases. */
export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) throw await toApiError(response);
  return (await response.json()) as T;
}

/** GET /api/health. A 503 still carries a body saying which dependency is down. */
export async function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const response = await fetch(`${API_URL}/api/health`, { cache: "no-store", signal });
  if (response.status !== 200 && response.status !== 503) throw await toApiError(response);
  return (await response.json()) as HealthResponse;
}
