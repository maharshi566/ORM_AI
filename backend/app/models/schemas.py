"""Pydantic models for API requests and responses.

What the agents themselves return (TriageResult, InvestigationResult, FinalResponse)
is in app/agents/schemas.py; these are the shapes the API sends and receives.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class DependencyStatus(BaseModel):
    status: Literal["ok", "error"]
    latency_ms: float | None = Field(default=None, description="How long the check took.")
    error: str | None = Field(default=None, description="Exception type when the check failed.")


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    app: str
    version: str
    environment: str
    checks: dict[str, DependencyStatus]


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: Any = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# --- Chat (Phase 4) -----------------------------------------------------------


class ChatRequest(BaseModel):
    shop_id: str = Field(pattern=r"^SHOP-\d{3}$", examples=["SHOP-001"])
    message: str = Field(min_length=1, max_length=2000, examples=["How much does CUST-0001 owe?"])
    session_id: str | None = Field(
        default=None,
        max_length=36,
        pattern=r"^[A-Za-z0-9-]+$",
        description="Leave empty to start a new conversation; send it back to continue one.",
    )
    user_id: str | None = Field(
        default=None, pattern=r"^USR-\d{3}$", description="Who is asking (login arrives in Phase 6)"
    )


class ReplyDetails(BaseModel):
    facts: list[str] = []
    evidence: list[str] = []
    next_steps: list[str] = []
    pending_approval: list[str] = []
    completed_actions: list[str] = []
    follow_up_question: str | None = None


class SourceView(BaseModel):
    citation: str
    title: str
    section: str
    excerpt: str
    trust: str


class ProposedActionView(BaseModel):
    action_id: str | None = None
    tool: str
    arguments: dict[str, Any]
    reason: str
    status: str = Field(
        description="proposed, awaiting_approval, approved, modified, rejected, blocked, "
        "needs_owner, done or failed (done and failed come only from the tool's result)"
    )
    required_role: str | None = None
    approval_id: str | None = None
    result: str | None = Field(default=None, description="What the tool returned, or why not")


class ApprovalActionView(BaseModel):
    approval_id: str
    action_id: str
    tool: str
    arguments: dict[str, Any]
    description: str
    reason: str
    required_role: Literal["staff", "owner"]
    approval_reasons: list[str] = []
    estimate: str | None = None


class ApprovalRequestView(BaseModel):
    """What a person is asked to decide: the actions, why, the evidence and the rules."""

    workflow_id: str
    required_role: Literal["staff", "owner"]
    summary: str = ""
    findings: list[str] = []
    evidence: list[dict[str, Any]] = []
    policy_references: list[str] = []
    confidence: float | None = None
    triggers: list[str] = []
    actions: list[ApprovalActionView]


class ToolActivity(BaseModel):
    agent: str
    tool: str
    status: str
    error_code: str | None = None
    latency_ms: float | None = None


class AgentStep(BaseModel):
    agent: str
    status: str
    model: str | None = None
    latency_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    summary: str
    error: str | None = None


class UsageTotals(BaseModel):
    input_tokens: int
    output_tokens: int
    llm_calls: int
    latency_ms: float


class ChatResponse(BaseModel):
    workflow_id: str
    session_id: str
    status: Literal["completed", "needs_clarification", "awaiting_approval", "blocked", "failed"]
    intent: str | None
    answer: str = Field(description="The reply, in Markdown")
    details: ReplyDetails
    sources: list[SourceView]
    proposed_actions: list[ProposedActionView]
    tool_calls: list[ToolActivity]
    agents: list[AgentStep]
    usage: UsageTotals
    validation: str | None
    warnings: list[str]
    errors: list[str]
    approval: ApprovalRequestView | None = Field(
        default=None, description="Set when status is awaiting_approval: what to decide"
    )


class ActionDecision(BaseModel):
    approval_id: str = Field(max_length=36)
    decision: Literal["approve", "reject", "modify"]
    arguments: dict[str, Any] | None = Field(
        default=None, description="For modify: the tool's arguments as they should be"
    )


class ApprovalDecisionRequest(BaseModel):
    user_id: str = Field(
        pattern=r"^USR-\d{3}$",
        examples=["USR-001"],
        description="Who decides (owner or staff of the workflow's shop; login in Phase 6)",
    )
    decision: Literal["approve", "reject", "modify"] | None = Field(
        default=None, description="One decision for every waiting action"
    )
    decisions: list[ActionDecision] = Field(
        default=[], max_length=20, description="Or one decision per action, by approval_id"
    )
    note: str | None = Field(default=None, max_length=500)


class ApprovalRecordView(BaseModel):
    approval_id: str
    tool: str
    status: str
    required_role: str | None
    decided_by: str | None
    decided_at: str | None
    decision_note: str | None


class ApprovalStatusResponse(BaseModel):
    workflow_id: str
    workflow_status: str
    approval: ApprovalRequestView | None
    approvals: list[ApprovalRecordView]


class GraphResponse(BaseModel):
    mermaid: str
