"""Pydantic models for API requests and responses.

Phase 0 has only the health and error shapes. Agent outputs (TriageResult,
InvestigationResult, ValidationResult, ...) are added here in Phase 4.
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
