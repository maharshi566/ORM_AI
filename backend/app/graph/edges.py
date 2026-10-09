"""Conditional edges: where the workflow goes after a routing node.

The routing decisions themselves are made inside the nodes (the supervisor, the
investigation agent's "decision node", and the validator), because they need the
settings, such as the loop limits. Each of those nodes writes its decision to
``state["route"]``; these functions read it and tell LangGraph which edge to follow.
The mappings list every allowed target, so the compiled graph (and its Mermaid
diagram) shows each possible path, and a route outside the mapping is an error
rather than a silent jump.
"""

from typing import Any

AFTER_SUPERVISOR = {
    "data_retrieval": "data_retrieval",
    "knowledge": "knowledge",
    "investigation": "investigation",
    "respond": "respond",
    "clarify": "clarify",
    "finalize": "finalize",
}
AFTER_INVESTIGATION = {  # the decision node
    "data_retrieval": "data_retrieval",  # need more data (at most AGENT_MAX_LOOPS times)
    "human_review": "human_review",  # actions or doubts: a person decides (Phase 5)
    "respond": "respond",  # enough to answer
}
AFTER_VALIDATION = {
    "respond": "respond",  # RETRY: rewrite with the validator's feedback
    "finalize": "finalize",  # PASS, or out of retries
}


def _route(state: dict[str, Any], allowed: dict[str, str], default: str) -> str:
    route = state.get("route") or default
    return route if route in allowed else default


def after_supervisor(state: dict[str, Any]) -> str:
    return _route(state, AFTER_SUPERVISOR, "finalize")


def after_investigation(state: dict[str, Any]) -> str:
    return _route(state, AFTER_INVESTIGATION, "respond")


def after_validation(state: dict[str, Any]) -> str:
    return _route(state, AFTER_VALIDATION, "finalize")
