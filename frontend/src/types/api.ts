// Shapes the ORM_AI backend sends and receives. Keep in sync with
// backend/app/models/schemas.py (the API's own description is at /api/openapi.json).

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

// ------------------------------------------------------------------ logins

export type Role = "owner" | "staff" | "admin";

export type TokenResponse = {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  user_id: string;
  shop_id: string | null;
  role: Role;
  name: string | null;
  shop_name: string | null;
};

export type MeResponse = {
  user_id: string | null;
  shop_id: string | null;
  role: Role | null;
  authenticated: boolean;
  name: string | null;
  shop_name: string | null;
};

export type DevUser = {
  user_id: string;
  name: string;
  role: Role;
  shop_id: string | null;
  shop_name: string | null;
};

// ------------------------------------------------------------------ chat

export type ChatStatus = "completed" | "needs_clarification" | "awaiting_approval" | "blocked" | "failed";

export type SourceView = {
  citation: string;
  title: string;
  section: string;
  excerpt: string;
  trust: string;
};

export type ProposedActionView = {
  action_id: string | null;
  tool: string;
  arguments: Record<string, unknown>;
  reason: string;
  status: string;
  required_role: string | null;
  approval_id: string | null;
  result: string | null;
};

export type ApprovalActionView = {
  approval_id: string;
  action_id: string;
  tool: string;
  arguments: Record<string, unknown>;
  description: string;
  reason: string;
  required_role: "staff" | "owner";
  approval_reasons: string[];
  estimate: string | null;
};

export type EvidenceItem = {
  source?: string;
  reference?: string;
  fact?: string;
  [key: string]: unknown;
};

export type ApprovalRequestView = {
  workflow_id: string;
  required_role: "staff" | "owner";
  summary: string;
  findings: string[];
  evidence: EvidenceItem[];
  policy_references: string[];
  confidence: number | null;
  triggers: string[];
  actions: ApprovalActionView[];
};

export type ToolActivity = {
  agent: string;
  tool: string;
  status: string;
  error_code: string | null;
  latency_ms: number | null;
};

export type AgentStep = {
  agent: string;
  status: string;
  model: string | null;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  summary: string;
  error: string | null;
};

export type ChatResponse = {
  workflow_id: string;
  session_id: string;
  status: ChatStatus;
  intent: string | null;
  answer: string;
  details: {
    facts: string[];
    evidence: string[];
    next_steps: string[];
    pending_approval: string[];
    completed_actions: string[];
    follow_up_question: string | null;
  };
  sources: SourceView[];
  proposed_actions: ProposedActionView[];
  tool_calls: ToolActivity[];
  agents: AgentStep[];
  usage: { input_tokens: number; output_tokens: number; llm_calls: number; latency_ms: number };
  validation: string | null;
  warnings: string[];
  errors: string[];
  approval: ApprovalRequestView | null;
};

export type ActionDecision = {
  approval_id: string;
  decision: "approve" | "reject" | "modify";
  arguments?: Record<string, unknown> | null;
};

export type ApprovalDecisionRequest = {
  decision?: "approve" | "reject";
  decisions?: ActionDecision[];
  note?: string;
};

export type ApprovalRecordView = {
  approval_id: string;
  tool: string;
  status: string;
  required_role: string | null;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
};

export type ApprovalStatusResponse = {
  workflow_id: string;
  workflow_status: string;
  approval: ApprovalRequestView | null;
  approvals: ApprovalRecordView[];
};

// ------------------------------------------------------------------ the live stream

export type StageEvent = {
  stage: string;
  agent: string;
  state: "started" | "finished";
  status?: string;
  summary?: string;
  latency_ms?: number;
  model?: string | null;
  error?: string | null;
};

export type StreamErrorEvent = { code: string; message: string; status: number };

// ------------------------------------------------------------------ records

export type MessageView = {
  role: string;
  content: string;
  created_at: string;
  workflow_id: string | null;
};

export type WorkflowSummary = {
  workflow_id: string;
  status: string;
  intent: string | null;
  user_query: string;
  created_at: string;
  completed_at: string | null;
};

export type SessionResponse = {
  session_id: string;
  shop_id: string;
  user_id: string | null;
  title: string | null;
  created_at: string;
  last_active_at: string;
  messages: MessageView[];
  workflows: WorkflowSummary[];
};

export type ShopView = {
  shop_id: string;
  name: string;
  shop_type: string;
  locality: string;
  city: string;
};

export type SessionListItem = {
  session_id: string;
  shop_id: string;
  user_id: string | null;
  title: string | null;
  created_at: string;
  last_active_at: string;
  workflows: number;
  last_status: string | null;
};

export type WorkflowListItem = WorkflowSummary & { shop_id: string; session_id: string | null };

export type AgentRunView = {
  agent: string;
  status: string;
  model: string | null;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  summary: string | null;
  error: string | null;
  started_at: string;
};

export type ToolCallView = {
  agent: string | null;
  tool: string;
  status: string;
  error_code: string | null;
  latency_ms: number;
  arguments: Record<string, unknown>;
  created_at: string;
};

export type WorkflowResponse = {
  workflow_id: string;
  session_id: string | null;
  shop_id: string;
  status: string;
  intent: string | null;
  user_query: string;
  final_response: string | null;
  error: string | null;
  created_at: string;
  updated_at: string | null;
  completed_at: string | null;
  agents: AgentRunView[];
  tool_calls: ToolCallView[];
  approvals: ApprovalRecordView[];
  approval: ApprovalRequestView | null;
  reply: ChatResponse | null;
};

export type ApprovalListItem = {
  approval_id: string;
  workflow_id: string;
  shop_id: string;
  tool: string;
  description: string;
  arguments: Record<string, unknown>;
  reason: string;
  required_role: string | null;
  approval_reasons: string[];
  estimate: string | null;
  confidence: number | null;
  policy_references: string[];
  status: string;
  user_query: string | null;
  workflow_status: string | null;
  created_at: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
};

export type ApprovalListResponse = {
  approvals: ApprovalListItem[];
  counts: Record<string, number>;
};

export type AgentMetrics = {
  agent: string;
  runs: number;
  errors: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
  input_tokens: number;
  output_tokens: number;
};

export type ToolMetrics = {
  tool: string;
  calls: number;
  errors: number;
  error_codes: Record<string, number>;
  avg_latency_ms: number;
};

export type MetricsResponse = {
  generated_at: string;
  scope: string;
  workflows: Record<string, number>;
  workflows_last_24h: number;
  agents: AgentMetrics[];
  tools: ToolMetrics[];
  approvals: Record<string, number>;
  documents: Record<string, number>;
};

export type EvaluationCaseView = {
  case_id: string;
  category: string;
  passed: boolean;
  scores: Record<string, number>;
  latency_ms: number | null;
  details: {
    message?: string;
    shop_id?: string;
    intent?: string | null;
    validation?: string | null;
    outcome?: string | null;
    asked?: string[];
    cited?: string[];
    missing_records?: string[];
    false_claims?: string[];
    errors?: string[];
    input_tokens?: number;
    output_tokens?: number;
    answer?: string;
    models?: string | null;
  };
};

export type EvaluationRunView = {
  run_id: string;
  created_at: string;
  models: string | null;
  cases: number;
  passed: number;
  scores: Record<string, number>;
  results: EvaluationCaseView[];
};

export type EvaluationsResponse = { runs: EvaluationRunView[] };
