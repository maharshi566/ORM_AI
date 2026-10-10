"""Human review: the policy gate, then a pause until a person decides.

Runs when the investigation proposed actions or asked for a review:

1. The policy gate (app/agents/policy_gate.py) decides, for each proposed action,
   whose approval it needs, whether it may run without asking (drafts and cases the
   shopkeeper asked for) and whether it may never run (based on an outside document).
2. If a person must decide, an ``approvals`` row is written for each action (status
   ``pending``), and the workflow **pauses** with LangGraph's ``interrupt()``. The chat
   reply then shows what is waiting, why, the evidence and the rules. The paused state
   is saved by the checkpointer, so it survives a restart.
3. ``POST /api/approval/{workflow_id}`` records the decision (approvals and audit_logs
   rows) and **resumes** the workflow with it. LangGraph runs this node again from its
   start; ``interrupt()`` then returns the decision instead of pausing. Writing the
   approvals rows is idempotent (fixed IDs, insert only), so running twice is safe.
4. Approved (or changed) actions go to the action agent; rejected ones never run.

Nothing here runs a tool. The decision comes only from the API, never from a model.
"""

import uuid
from typing import Any

from langgraph.types import interrupt
from pydantic import ValidationError

from app.agents import policy_gate
from app.agents.common import AgentOutcome, ids_in, known_ids
from app.core.logging import get_logger
from app.graph.deps import AgentDeps
from app.models import Approval, AuditLog

logger = get_logger(__name__)

AGENT = "human_review"
# Approval IDs are derived from the workflow and the action, so the node can run twice
# (pause, then resume) and still write each approval once.
APPROVAL_NAMESPACE = uuid.UUID("6f3c2a1e-9d4b-4c7e-8a55-0b1d2e3f4a5b")
WAITING = "awaiting_approval"
RUNNABLE = frozenset({"approved", "modified"})


def approval_id(workflow_id: str, action_id: str) -> str:
    return str(uuid.uuid5(APPROVAL_NAMESPACE, f"{workflow_id}:{action_id}"))


def _shown_arguments(action: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in action["arguments"].items() if k != "idempotency_key"}


def describe(action: dict[str, Any]) -> str:
    shown = ", ".join(f"{k}={v}" for k, v in _shown_arguments(action).items())
    return f"{action['tool']} ({shown})"


def _safe_summary(state: dict[str, Any]) -> str:
    """The investigation's conclusion, unless it names a record no tool returned."""
    investigation = state.get("investigation_result") or {}
    for text in (investigation.get("summary"), investigation.get("recommended_action")):
        if text and not (ids_in(text) - known_ids(state)):
            return str(text)
    return ""


def pause_message(request: dict[str, Any]) -> str:
    """The reply shown while the workflow waits: what, who decides, why, which rules."""
    lines = ["I need a decision before I do anything."]
    if request.get("summary"):
        lines.append(request["summary"])
    lines.append("\n**Waiting for approval**")
    for action in request["actions"]:
        who = "the owner" if action["required_role"] == "owner" else "staff or the owner"
        line = f"- {action['description']}: {action['reason']} Needs {who}"
        if action.get("approval_reasons"):
            line += f" ({'; '.join(action['approval_reasons'])})"
        if action.get("estimate"):
            line += f"; {action['estimate']}"
        lines.append(line + ".")
    if request.get("triggers"):
        lines.append("\nWhy a person must decide: " + "; ".join(request["triggers"]) + ".")
    if request.get("policy_references"):
        lines.append("\nRules: " + " ".join(request["policy_references"]))
    lines.append(
        "\nNothing has been changed yet. Approve, change or reject it in the approval panel "
        "(or POST /api/approval/{workflow_id})."
    )
    return "\n".join(lines)


def build_request(
    state: dict[str, Any], pending: list[dict[str, Any]], gate: policy_gate.GateResult
) -> dict[str, Any]:
    investigation = state.get("investigation_result") or {}
    actions = [
        {
            "approval_id": a["approval_id"],
            "action_id": a["action_id"],
            "tool": a["tool"],
            "arguments": _shown_arguments(a),
            "description": describe(a),
            "reason": a["reason"],
            "required_role": a["required_role"],
            "approval_reasons": a.get("approval_reasons") or [],
            "estimate": a.get("estimate"),
        }
        for a in pending
    ]
    request = {
        "type": "approval_request",
        "workflow_id": state["workflow_id"],
        "shop_id": state["shop_id"],
        "summary": _safe_summary(state),
        "findings": investigation.get("findings") or [],
        "evidence": investigation.get("evidence") or [],
        "policy_references": investigation.get("policy_references") or [],
        "confidence": state.get("confidence"),
        "triggers": [*gate.triggers, *gate.review],
        "required_role": "owner"
        if any(a["required_role"] == "owner" for a in actions)
        else "staff",
        "actions": actions,
    }
    request["message"] = pause_message(request).replace("{workflow_id}", state["workflow_id"])
    return request


async def record_pending(deps: AgentDeps, state: dict[str, Any], request: dict[str, Any]) -> None:
    """One approvals row per waiting action. Insert only: a decided row is never reset."""
    if not deps.record_to_db:
        return
    async with deps.session_factory() as session:
        for action in request["actions"]:
            if await session.get(Approval, action["approval_id"]) is not None:
                continue
            session.add(
                Approval(
                    id=action["approval_id"],
                    workflow_id=state["workflow_id"],
                    shop_id=state["shop_id"],
                    action_type=action["tool"],
                    proposed_action={
                        "action_id": action["action_id"],
                        "tool": action["tool"],
                        "arguments": action["arguments"],
                        "required_role": action["required_role"],
                        "approval_reasons": action["approval_reasons"],
                        "estimate": action["estimate"],
                    },
                    reason=action["reason"],
                    evidence=request["evidence"],
                    policy_refs=request["policy_references"],
                    confidence=float(request["confidence"] or 0.0),
                    status="pending",
                )
            )
            session.add(
                AuditLog(
                    actor_type="agent",
                    actor_id=AGENT,
                    action="approval_requested",
                    entity_type="approval",
                    entity_id=action["approval_id"][:40],
                    shop_id=state["shop_id"],
                    workflow_id=state["workflow_id"],
                    details={"tool": action["tool"], "required_role": action["required_role"]},
                )
            )
        await session.commit()


def reject_all(request: dict[str, Any], *, decided_by: str, note: str) -> dict[str, Any]:
    """A decision that rejects every waiting action (used by the evaluation script)."""
    return {
        "decided_by": decided_by,
        "role": "owner",
        "note": note,
        "decided_at": None,
        "decisions": {
            a["action_id"]: {
                "decision": "reject",
                "arguments": None,
                "approval_id": a["approval_id"],
            }
            for a in request["actions"]
        },
    }


def apply_decision(
    actions: list[dict[str, Any]], decision: dict[str, Any], deps: AgentDeps
) -> list[dict[str, Any]]:
    """The person's decision applied to the waiting actions. Undecided means not run."""
    choices = decision.get("decisions") or {}
    decided_by, role = decision.get("decided_by"), decision.get("role")
    for action in actions:
        if action["status"] != WAITING:
            continue
        choice = choices.get(action["action_id"]) or {}
        verdict = choice.get("decision")
        if verdict not in {"approve", "modify"}:
            action["status"] = "rejected"
            action["result"] = (
                f"Rejected by {decided_by}." if verdict == "reject" else "No decision was given."
            )
            if decision.get("note"):
                action["result"] += f" Note: {decision['note']}"
            continue
        if action["required_role"] == "owner" and role != "owner":
            action["status"] = "needs_owner"
            action["result"] = "Only the shop owner can approve this."
            continue
        if verdict == "modify":
            spec = deps.registry.spec(action["tool"])
            changed = {**(choice.get("arguments") or {})}
            key = action["arguments"].get("idempotency_key")
            if key:
                changed["idempotency_key"] = f"{key}:m"[:80]
            try:
                action["arguments"] = spec.input_model.model_validate(changed).model_dump(
                    mode="json"
                )  # type: ignore[union-attr]
            except (ValidationError, AttributeError) as exc:
                action["status"] = "rejected"
                action["result"] = f"The changed details were not valid ({type(exc).__name__})."
                continue
        action["status"] = "modified" if verdict == "modify" else "approved"
        action["approved_by"], action["approver_role"] = decided_by, role
    return actions


async def run(state: dict[str, Any], deps: AgentDeps) -> AgentOutcome:
    actions = [dict(a) for a in state.get("proposed_actions") or []]
    if not actions:
        return AgentOutcome(
            update={
                "route": "respond",
                "warnings": [
                    "The investigation asked for a person to review this, but proposed no "
                    "action; the reply explains what was found."
                ],
            },
            summary="review asked; nothing to approve",
        )

    async with deps.session_factory() as session:
        gate = await policy_gate.assess(actions, state, session, deps.now)
    wants_action = bool((state.get("triage") or {}).get("wants_action"))
    pending: list[dict[str, Any]] = []
    for action in actions:
        check = gate.checks[action["action_id"]]
        action["approval_reasons"] = check.reasons
        if check.estimate:
            action["estimate"] = check.estimate
        if check.blocked:
            action.update(
                status="blocked", required_role=None, result=f"Not allowed: {check.blocked}."
            )
        elif gate.runs_without_asking(action["action_id"], wants_action=wants_action):
            action.update(status="approved", required_role=None, approved_by="policy")
        else:
            action.update(
                status=WAITING,
                required_role=gate.role_for(action["action_id"]),
                approval_id=approval_id(state["workflow_id"], action["action_id"]),
            )
            pending.append(action)

    if not pending:
        runnable = [a for a in actions if a["status"] in RUNNABLE]
        return AgentOutcome(
            update={"proposed_actions": actions, "route": "action" if runnable else "respond"},
            summary=f"{len(runnable)} low-risk action(s) run without asking, "
            f"{len(actions) - len(runnable)} not allowed",
        )

    request = build_request(state, pending, gate)
    await record_pending(deps, state, request)
    await deps.emit({"type": "approval_requested", "workflow_id": state["workflow_id"]})
    decision = interrupt(request)  # pauses here; on resume, returns the person's decision

    actions = apply_decision(actions, decision, deps)
    runnable = [a for a in actions if a["status"] in RUNNABLE]
    counts = {
        status: sum(a["status"] == status for a in actions)
        for status in ("approved", "modified", "rejected", "needs_owner", "blocked")
    }
    human = {
        "decided_by": decision.get("decided_by"),
        "role": decision.get("role"),
        "note": decision.get("note"),
        "decided_at": decision.get("decided_at"),
        **counts,
    }
    logger.info("approval_applied", workflow_id=state["workflow_id"], **counts)
    return AgentOutcome(
        update={
            "proposed_actions": actions,
            "approval_request": request,
            "human_approval": human,
            "route": "action" if runnable else "respond",
        },
        summary=f"decided by {human['decided_by']} ({human['role']}): "
        + ", ".join(f"{n} {status}" for status, n in counts.items() if n),
    )
