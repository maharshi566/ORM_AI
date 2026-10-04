// Shapes returned by the ORM_AI backend. Keep in sync with backend/app/models/schemas.py.

export type DependencyStatus = {
  status: "ok" | "error";
  latency_ms: number | null;
  error: string | null;
};

export type HealthResponse = {
  status: "ok" | "degraded";
  app: string;
  version: string;
  environment: string;
  checks: Record<string, DependencyStatus>;
};

export type ApiErrorBody = {
  error: {
    code: string;
    message: string;
    request_id: string | null;
    details: unknown;
  };
};
