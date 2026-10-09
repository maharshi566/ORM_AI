"""Response agent: writes the reply the shopkeeper reads.

Receives the request, the records, the investigation (if any), the policy passages,
the proposed actions and any errors, and returns a ``FinalResponse`` (smart model,
structured output): a direct answer, then facts, applicable rules, next steps, what
waits for approval, and the citations used.

Two things are decided by code, not by the model:

* **Completed actions.** Only an action tool's success counts. Phase 4 runs no
  action tools, so the reply never lists anything as done.
* **Pending approval.** Taken from the validated proposed actions in the state. The
  model may word them, but cannot add or remove one.

When the validator sends the draft back (RETRY), its feedback is included and the
model rewrites the reply.
"""

import json
from typing import Any

from app.agents.common import (
    AgentOutcome,
    compact,
    render_passages,
    render_records,
    request_header,
)
from app.agents.schemas import FinalResponse
from app.graph.deps import AgentDeps
from app.prompts.response_prompt import RESPONSE_PROMPT

AGENT = "respond"


def describe_action(action: dict[str, Any]) -> str:
    """'follow_up_supplier (purchase_order_id=PO-00585, issue=late): reason'."""
    arguments = {k: v for k, v in action["arguments"].items() if k != "idempotency_key"}
    shown = ", ".join(
        f"{k}={json.dumps(v) if isinstance(v, list | dict) else v}" for k, v in arguments.items()
    )
    return f"{action['tool']} ({shown}): {action['reason']}"


def _task(state: dict[str, Any], deps: AgentDeps) -> str:
    parts = [request_header(state, deps.now), render_records(state), render_passages(state)]
    investigation = state.get("investigation_result")
    if investigation:
        shown = {
            key: investigation[key]
            for key in ("issue_type", "summary", "findings", "evidence", "policy_references")
            if investigation.get(key)
        }
        shown["recommended_action"] = investigation.get("recommended_action")
        parts.append(f"<investigation>\n{compact(shown, 5000)}\n</investigation>")
    actions = state.get("proposed_actions") or []
    parts.append(
        "Proposed actions (NOT done; waiting for approval):\n- "
        + "\n- ".join(describe_action(a) for a in actions)
        if actions
        else "Proposed actions: none. Nothing has been done or changed."
    )
    problems = (state.get("errors") or []) + (state.get("warnings") or [])
    if problems:
        parts.append("Problems while gathering evidence:\n- " + "\n- ".join(problems[-8:]))
    if state.get("validation_feedback"):
        parts.append(
            "Your previous draft was rejected. Fix these problems:\n- "
            + "\n- ".join(state["validation_feedback"])
        )
    parts.append("Write the reply.")
    return "\n\n".join(parts)


async def run(state: dict[str, Any], deps: AgentDeps) -> AgentOutcome:
    retry = bool(state.get("validation_feedback"))
    reply = await deps.llm.structured(
        FinalResponse,
        system=RESPONSE_PROMPT.render(),
        messages=[{"role": "user", "content": _task(state, deps)}],
        tier="smart",
    )
    draft = reply.value.model_dump()
    actions = state.get("proposed_actions") or []
    if not actions:
        draft["pending_approval"] = []
    elif len(draft["pending_approval"]) != len(actions):
        draft["pending_approval"] = [describe_action(a) for a in actions]
    draft["completed_actions"] = []  # Phase 5: filled from successful action tool results
    update: dict[str, Any] = {"draft_response": draft, "completed_steps": [AGENT]}
    if retry:
        update["response_retries"] = (state.get("response_retries") or 0) + 1
    citations = len(draft["citations"])
    return AgentOutcome(
        update=update,
        summary=f"{'rewrote' if retry else 'wrote'} the reply: {len(draft['answer'])} "
        f"characters, {citations} citation(s), {len(draft['pending_approval'])} pending",
        usage=reply.usage,
    )
