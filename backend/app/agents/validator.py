"""Validator: checks the draft reply before the shopkeeper sees it.

Phase 4 runs these checks in code, which is fast, free and cannot be argued with:

1. every citation in the reply is the citation of a passage actually retrieved;
2. a question about a shop rule cites one, when passages were found;
3. every record ID in the reply (PRD-0002, CUST-0001, ...) was written by the user or
   returned by a tool, so the reply cannot point at a record that does not exist;
4. the reply never says ORM_AI did something ("I have sent the reminder") unless an
   action tool reported success (none run before Phase 5);
5. the answer is not empty.

Decision: PASS when all checks pass. RETRY sends the problems back to the response
agent, at most AGENT_MAX_LOOPS times. After that the decision is HUMAN_REVIEW: the
reply goes out with unknown citations removed and a note that it could not be fully
verified. Phase 5 adds the policy gate (BLOCK for answers that follow instructions
found in untrusted documents) and makes HUMAN_REVIEW pause for a person.
"""

import re
from typing import Any

from app.agents.common import AgentOutcome, allowed_citations, citations_in, ids_in, known_ids
from app.graph.deps import AgentDeps

AGENT = "validate"

# Intents whose answer should rest on a shop rule.
POLICY_INTENTS = frozenset(
    {
        "reorder",
        "customer_credit",
        "payment_reminder",
        "supplier_issue",
        "stock_discrepancy",
        "pricing",
        "returns",
        "policy_question",
    }
)
# First-person claims of having acted: "I have sent", "I've placed", "we recorded".
DONE_CLAIM = re.compile(
    r"\b(?:i|we|orm_ai)(?:\s+have|'ve)?\s+(?:just\s+|already\s+)?"
    r"(?:sent|placed|created|recorded|updated|refunded|processed|messaged|ordered|"
    r"changed|adjusted|issued|submitted|cancelled)\b",
    re.IGNORECASE,
)


def draft_text(draft: dict[str, Any], *, include_pending: bool = True) -> str:
    parts = [draft.get("answer") or "", *draft.get("facts", []), *draft.get("evidence", [])]
    parts += draft.get("next_steps", [])
    if include_pending:
        parts += draft.get("pending_approval", [])
    parts.append(draft.get("follow_up_question") or "")
    return "\n".join(parts)


def find_problems(draft: dict[str, Any], state: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if not (draft.get("answer") or "").strip():
        problems.append("The answer is empty. Answer the question directly.")
    text = draft_text(draft)
    allowed = allowed_citations(state)
    used = set(citations_in(text)) | set(draft.get("citations") or [])
    unknown = sorted(c for c in used if c not in allowed)
    if unknown:
        problems.append(
            "These citations are not among the retrieved passages; use only the given "
            f"citations, copied exactly: {', '.join(unknown)}"
        )
    if state.get("intent") in POLICY_INTENTS and allowed and not (used & allowed):
        problems.append(
            "The reply relies on shop rules but cites none. Cite the passage you used, "
            "for example " + sorted(allowed)[0]
        )
    unknown_ids = sorted(ids_in(text) - known_ids(state))
    if unknown_ids:
        problems.append(
            "These IDs do not appear in the request or the records; remove them or use "
            f"the IDs from the records: {', '.join(unknown_ids)}"
        )
    actions_done = [
        r
        for r in state.get("tool_results") or []
        if r.get("agent") == "action" and r["status"] == "success"
    ]
    if not actions_done and DONE_CLAIM.search(draft_text(draft, include_pending=False)):
        problems.append(
            "The reply says an action was done, but no action was carried out. Describe "
            "it as proposed and waiting for approval."
        )
    return problems


async def run(state: dict[str, Any], deps: AgentDeps) -> AgentOutcome:
    draft = state.get("draft_response")
    if not draft:
        return AgentOutcome(
            update={"validation_result": None, "route": "finalize"},
            summary="no draft to check (the response agent failed)",
        )
    problems = find_problems(draft, state)
    retries = state.get("response_retries") or 0
    if not problems:
        decision, route = "PASS", "finalize"
    elif retries < deps.settings.agent_max_loops:
        decision, route = "RETRY", "respond"
    else:
        decision, route = "HUMAN_REVIEW", "finalize"
    update: dict[str, Any] = {
        "validation_result": decision,
        "validation_feedback": problems,
        "route": route,
    }
    if decision == "HUMAN_REVIEW":
        update["warnings"] = [f"Answer not fully verified: {p}" for p in problems]
    return AgentOutcome(
        update=update,
        summary=decision + (f": {len(problems)} problem(s)" if problems else ""),
    )
