"""Platform tables: what the AI system records about its own work.

These hold conversations, workflow runs, every agent step and tool call, the
documents behind RAG, approvals, the audit trail and evaluation results. They
power memory (Phase 4), approvals (Phase 5), the admin page (Phase 7) and the
evaluation report (Phase 8).
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base
from app.models.types import (
    ActorType,
    ApprovalStatus,
    JSONType,
    MessageRole,
    RunStatus,
    UserRole,
    WorkflowStatus,
    enum_type,
    utcnow,
)


def _now_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # USR-001
    shop_id: Mapped[str | None] = mapped_column(ForeignKey("shops.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    role: Mapped[UserRole] = mapped_column(enum_type(UserRole, "user_role"))
    phone: Mapped[str | None] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _now_column()


class ChatSession(Base):
    """One conversation between a shop user and ORM_AI (short-term memory)."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _now_column()
    last_active_at: Mapped[datetime] = _now_column()


class Workflow(Base):
    """One run of the agent graph for one user request."""

    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"), index=True)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    user_query: Mapped[str] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[WorkflowStatus] = mapped_column(enum_type(WorkflowStatus, "workflow_status"))
    final_response: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    workflow_id: Mapped[str | None] = mapped_column(ForeignKey("workflows.id"))
    role: Mapped[MessageRole] = mapped_column(enum_type(MessageRole, "message_role"))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_column()


class AgentRun(Base):
    """One agent step inside a workflow: who ran, how long, how many tokens."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), index=True)
    agent: Mapped[str] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[RunStatus] = mapped_column(enum_type(RunStatus, "run_status"))
    latency_ms: Mapped[float | None] = mapped_column(Float)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    # A short decision summary, never hidden reasoning.
    summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = _now_column()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolCall(Base):
    """Every tool call, with its exact arguments and result."""

    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str | None] = mapped_column(ForeignKey("workflows.id"), index=True)
    agent: Mapped[str | None] = mapped_column(String(40))
    tool_name: Mapped[str] = mapped_column(String(60), index=True)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONType)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    status: Mapped[RunStatus] = mapped_column(enum_type(RunStatus, "tool_status"))
    error_code: Mapped[str | None] = mapped_column(String(40))
    latency_ms: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = _now_column()


class Document(Base):
    """One version of one knowledge-base document."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(60), primary_key=True)  # POL-CREDIT-001@v2
    document_id: Mapped[str] = mapped_column(String(40), index=True)
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(40), index=True)
    shop_id: Mapped[str | None] = mapped_column(ForeignKey("shops.id"))
    effective_date: Mapped[date] = mapped_column(Date)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    path: Mapped[str] = mapped_column(String(300))
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # content hash
    document_pk: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(200))
    page: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now_column()


class Approval(Base):
    """A consequential action waiting for, or decided by, a person."""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), index=True)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(60))
    proposed_action: Mapped[dict[str, Any]] = mapped_column(JSONType)
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[Any]] = mapped_column(JSONType)
    policy_refs: Mapped[list[Any]] = mapped_column(JSONType)
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[ApprovalStatus] = mapped_column(enum_type(ApprovalStatus, "approval_status"))
    decided_by: Mapped[str | None] = mapped_column(String(40))
    decision_note: Mapped[str | None] = mapped_column(Text)
    final_action: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = _now_column()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Append-only record of who did what, for every consequential change."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_type: Mapped[ActorType] = mapped_column(enum_type(ActorType, "actor_type"))
    actor_id: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(60))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(40))
    shop_id: Mapped[str | None] = mapped_column(ForeignKey("shops.id"), index=True)
    workflow_id: Mapped[str | None] = mapped_column(ForeignKey("workflows.id"), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType)
    created_at: Mapped[datetime] = _now_column()


class Evaluation(Base):
    """One evaluation case result from one evaluation run (Phase 8)."""

    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    case_id: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(40))
    passed: Mapped[bool] = mapped_column(Boolean)
    scores: Mapped[dict[str, Any]] = mapped_column(JSONType)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = _now_column()
