"""Shared, typed state that flows through every node of the LangGraph workflow.

Fields marked with ``operator.add`` are appended to rather than replaced when a
node returns them. Nested values are plain dicts for now; they become Pydantic
models (TriageResult, InvestigationResult, ...) in Phase 4.
"""

import operator
from typing import Annotated, Any, Literal, TypedDict

ValidationDecision = Literal["PASS", "RETRY", "HUMAN_REVIEW", "BLOCK"]


class AgentState(TypedDict, total=False):
    # Identity
    workflow_id: str
    session_id: str
    user_id: str
    shop_id: str

    # Input and memory
    user_query: str
    conversation_history: Annotated[list[dict[str, Any]], operator.add]

    # Triage
    intent: str | None
    entities: dict[str, Any]
    missing_information: list[str]

    # Evidence
    retrieved_data: dict[str, Any]
    retrieved_documents: list[dict[str, Any]]
    investigation_result: dict[str, Any] | None
    confidence: float | None

    # Actions
    proposed_actions: list[dict[str, Any]]
    human_approval: dict[str, Any] | None
    tool_results: Annotated[list[dict[str, Any]], operator.add]

    # Output
    validation_result: ValidationDecision | None
    final_response: str | None
    errors: Annotated[list[str], operator.add]

    # Loop guards
    retrieval_loops: int
    response_retries: int
