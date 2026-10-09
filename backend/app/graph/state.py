"""The shared state every node of the agent graph reads and updates.

LangGraph passes one ``AgentState`` dict through the graph. Each node returns only the
keys it changes. Most keys are simply replaced; the ones marked with a *reducer*
(``Annotated[..., reducer]``) are combined instead, so that results from several
visits add up rather than overwrite each other:

* ``operator.add`` appends lists (the trace, tool results, errors);
* ``merge_records`` keeps every record fetched, across data-retrieval visits;
* ``merge_passages`` keeps each policy passage once, by its citation.

Only plain JSON-like values (dicts, lists, strings, numbers) are stored, because the
state is saved to the database after every step (see checkpointer.py).
"""

import operator
from typing import Annotated, Any, Literal, TypedDict

ValidationDecision = Literal["PASS", "RETRY", "HUMAN_REVIEW", "BLOCK"]
WorkflowOutcome = Literal["completed", "needs_clarification", "failed"]
Route = Literal[
    "data_retrieval",
    "knowledge",
    "investigation",
    "human_review",
    "respond",
    "clarify",
    "finalize",
]


def merge_records(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """Records fetched so far, keyed by tool call; a newer fetch of the same key wins."""
    return {**(left or {}), **(right or {})}


def merge_passages(
    left: list[dict[str, Any]] | None, right: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Policy passages, one per citation, best score first."""
    by_citation: dict[str, dict[str, Any]] = {}
    for passage in [*(left or []), *(right or [])]:
        kept = by_citation.get(passage["citation"])
        if kept is None or passage.get("score", 0) > kept.get("score", 0):
            by_citation[passage["citation"]] = passage
    return sorted(by_citation.values(), key=lambda p: p.get("score", 0), reverse=True)


class AgentState(TypedDict, total=False):
    # --- Who and what (set by the chat service before the graph starts) ----
    workflow_id: str
    session_id: str
    user_id: str | None
    shop_id: str
    user_query: str
    conversation_history: Annotated[list[dict[str, Any]], operator.add]  # earlier turns

    # --- Triage ------------------------------------------------------------
    triage: dict[str, Any] | None  # the full TriageResult
    intent: str | None
    entities: dict[str, Any]
    missing_information: list[str]

    # --- Routing -----------------------------------------------------------
    plan: list[str]  # specialists the supervisor chose: "data", "knowledge", "investigation"
    route: Route  # where the last routing node sends the workflow next
    completed_steps: Annotated[list[str], operator.add]  # one entry per agent visit
    retrieval_loops: int  # times the investigation asked for more data
    response_retries: int  # times the validator asked for a rewrite

    # --- Evidence ----------------------------------------------------------
    data_requests: list[str]  # extra records the investigation asked for
    retrieved_data: Annotated[dict[str, Any], merge_records]
    retrieved_documents: Annotated[list[dict[str, Any]], merge_passages]
    tool_results: Annotated[list[dict[str, Any]], operator.add]  # every call, ok or not

    # --- Investigation and actions -------------------------------------------
    investigation_result: dict[str, Any] | None
    confidence: float | None
    proposed_actions: list[dict[str, Any]]  # validated, not executed (Phase 5 executes)
    human_approval: dict[str, Any] | None  # Phase 5: the person's decision

    # --- Answer ------------------------------------------------------------
    draft_response: dict[str, Any] | None  # the FinalResponse being validated
    validation_result: ValidationDecision | None
    validation_feedback: list[str]
    final_response: str | None  # the reply shown to the shopkeeper (Markdown)
    sources: list[dict[str, Any]]  # the passages the reply cites
    outcome: WorkflowOutcome

    # --- Bookkeeping -------------------------------------------------------
    agent_trace: Annotated[list[dict[str, Any]], operator.add]  # one entry per agent run
    failed_agents: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]  # handled problems worth showing
    errors: Annotated[list[str], operator.add]  # problems that changed the answer
