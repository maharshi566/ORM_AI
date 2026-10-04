"""The contract every tool follows.

A tool is a plain async function plus a ``ToolSpec`` that describes it: a name, a
description written for the LLM, a Pydantic input model, a Pydantic output model,
a timeout and a kind (read or action). The registry (registry.py) is the only
thing that runs tools, and it always returns a ``ToolResult``: it never raises, so
an agent always gets a clear success or a clear error code, never a crash and
never a made-up value.

Three safety rules are built into the context rather than left to the LLM:

* **Shop scope.** ``ToolContext.shop_id`` comes from the logged-in user, not from
  tool arguments, so a tool can never read or change another shop's records.
* **Approval.** ``ToolContext.approval`` is set only by the human-approval step
  (Phase 5). Action tools check it themselves, so a consequential action cannot
  run just because the LLM asked for it.
* **Today.** ``ToolContext.now`` is the business clock, so "days overdue" is
  reproducible in tests and against the synthetic data.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

IST = timezone(timedelta(hours=5, minutes=30), "IST")


class ToolErrorCode(StrEnum):
    UNKNOWN_TOOL = "unknown_tool"
    FORBIDDEN = "forbidden"  # this agent may not use this tool
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"  # e.g. an open order already exists
    POLICY_BLOCKED = "policy_blocked"  # a shop rule forbids it
    APPROVAL_REQUIRED = "approval_required"  # a person must approve first
    TIMEOUT = "timeout"
    UPSTREAM_ERROR = "upstream_error"  # an external API failed
    RATE_LIMITED = "rate_limited"
    INTERNAL = "internal_error"


RETRYABLE = {ToolErrorCode.TIMEOUT, ToolErrorCode.UPSTREAM_ERROR, ToolErrorCode.RATE_LIMITED}


class ToolError(Exception):
    """Raised inside a tool for an expected failure. The registry turns it into a result."""

    def __init__(
        self,
        code: ToolErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = code in RETRYABLE if retryable is None else retryable


class ToolResult(BaseModel):
    """What every tool call returns to an agent."""

    tool: str
    status: Literal["success", "error"]
    data: dict[str, Any] | None = None
    error_code: ToolErrorCode | None = None
    error_message: str | None = None
    error_details: dict[str, Any] | None = None
    retryable: bool = False
    latency_ms: float = 0.0
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.status == "success"


class ToolInput(BaseModel):
    """Base for tool inputs: unknown fields are rejected, not silently ignored."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class ApprovalGrant:
    """Proof that a person approved this action. Only the approval step creates it."""

    approved_by: str  # user ID
    role: Literal["owner", "staff"]
    approval_id: str | None = None

    def satisfies(self, needed: Literal["owner", "staff"]) -> bool:
        return self.role == "owner" or needed == "staff"


@dataclass
class ToolContext:
    session: AsyncSession
    shop_id: str
    actor: str  # agent name or user ID, recorded with every change
    now: datetime = field(default_factory=lambda: datetime.now(IST))
    workflow_id: str | None = None
    approval: ApprovalGrant | None = None
    # Separate sessions for writing tool-call logs, so a failed action's log survives
    # the action's rollback. None means "log only to the application log".
    log_session_factory: async_sessionmaker[AsyncSession] | None = None
    clients: dict[str, Any] = field(default_factory=dict)  # mock external APIs

    @property
    def today(self) -> date:
        return self.now.date()

    def require_approval(self, needed: Literal["owner", "staff"], reason: str) -> None:
        if self.approval is None or not self.approval.satisfies(needed):
            raise ToolError(
                ToolErrorCode.APPROVAL_REQUIRED,
                f"This needs approval from the shop {needed}: {reason}",
                details={"required_role": needed, "reason": reason},
            )


def business_now(business_date: date | None) -> datetime:
    """The current time in IST, on ``business_date`` when one is configured.

    Freezing the date keeps "days overdue" stable against the synthetic data.
    """
    now = datetime.now(IST)
    if business_date is None:
        return now
    return now.replace(year=business_date.year, month=business_date.month, day=business_date.day)


Handler = Callable[[ToolContext, Any], Awaitable[BaseModel]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[ToolInput]
    output_model: type[ToolOutput]
    handler: Handler
    kind: Literal["read", "action"]
    timeout_seconds: float = 5.0
    max_retries: int = 0  # automatic retries for retryable errors (reads only)

    def json_schema(self) -> dict[str, Any]:
        """OpenAI-style function definition, used when agents call tools (Phase 4)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }
