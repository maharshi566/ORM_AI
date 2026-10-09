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
    tool: str
    arguments: dict[str, Any]
    reason: str
    status: str


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
    status: Literal["completed", "needs_clarification", "failed"]
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


class GraphResponse(BaseModel):
    mermaid: str
