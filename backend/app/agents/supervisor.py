"""Supervisor: decides which specialist runs next. Rules in code, no LLM call.

The graph comes back here after every specialist (a hub-and-spoke design), and the
supervisor picks the next step from the triage result and what has already run:

1. triage failed                        -> finalize (explain the failure)
2. out of scope                         -> finalize (a polite fixed reply)
3. needs clarification                  -> clarify (ask the triage question)
4. records needed and not fetched yet   -> data_retrieval
5. rules needed and not searched yet    -> knowledge
6. a problem to work out, or new data   -> investigation
7. otherwise                            -> respond

Why rules and not a model? Routing must be predictable, cheap and testable, and the
judgement it needs (what is being asked) is already in the triage result. The triage
agent's ``recommended_route`` can add specialists, never remove the ones an intent
requires.
"""

from typing import Any

from app.agents.common import AgentOutcome
from app.graph.deps import AgentDeps

# Which specialists each intent needs. "data" = shop records, "knowledge" = shop rules.
INTENT_PLAN: dict[str, list[str]] = {
    "stock_status": ["data"],
    "reorder": ["data", "knowledge"],
    "sales_report": ["data"],
    "customer_credit": ["data", "knowledge"],
    "payment_reminder": ["data", "knowledge"],
    "supplier_issue": ["data", "knowledge"],
    "stock_discrepancy": ["data", "knowledge"],
    "pricing": ["data", "knowledge"],
    "returns": ["data", "knowledge"],
    "policy_question": ["knowledge"],
    "general_help": ["knowledge"],
    "out_of_scope": [],
}

# Intents that describe a problem to work out, so the investigation agent runs.
INVESTIGATE: frozenset[str] = frozenset(
    {
        "reorder",
        "customer_credit",
        "payment_reminder",
        "supplier_issue",
        "stock_discrepancy",
        "pricing",
        "returns",
    }
)

OUT_OF_SCOPE_REPLY = (
    "I can help with this shop's stock, sales, purchase orders, suppliers, customer "
    "credit (udhaar) and shop rules. Could you ask me something about those?"
)


def make_plan(triage: dict[str, Any]) -> list[str]:
    intent = triage.get("intent") or "general_help"
    wanted = set(INTENT_PLAN.get(intent, ["data", "knowledge"]))
    if intent != "out_of_scope":
        wanted |= set(triage.get("recommended_route") or [])
    plan = [step for step in ("data", "knowledge") if step in wanted]
    if intent in INVESTIGATE or (triage.get("wants_action") and intent != "out_of_scope"):
        plan.append("investigation")
    return plan


def next_step(state: dict[str, Any], plan: list[str]) -> tuple[str, str]:
    """(route, reason) for the current state."""
    triage = state.get("triage")
    if triage is None:
        return "finalize", "triage failed, so the request could not be understood"
    if triage.get("intent") == "out_of_scope":
        return "finalize", "out of scope"
    if triage.get("needs_clarification") and triage.get("clarifying_question"):
        return "clarify", "the request needs clarification first"
    done = state.get("completed_steps") or []
    data_runs, investigations = done.count("data_retrieval"), done.count("investigation")
    if "data" in plan and data_runs == 0:
        return "data_retrieval", "records needed"
    if "knowledge" in plan and "knowledge" not in done:
        return "knowledge", "shop rules needed"
    new_data = investigations < data_runs  # the investigation asked for more, and got it
    if "investigation" in plan and (investigations == 0 or new_data):
        return "investigation", "a problem to work out"
    return "respond", "enough evidence to answer"


async def run(state: dict[str, Any], deps: AgentDeps) -> AgentOutcome:
    update: dict[str, Any] = {}
    plan = state.get("plan")
    if plan is None and state.get("triage") is not None:
        plan = make_plan(state["triage"])
        update["plan"] = plan
    route, reason = next_step(state, plan or [])
    update["route"] = route
    if route == "finalize" and (state.get("triage") or {}).get("intent") == "out_of_scope":
        update["final_response"] = OUT_OF_SCOPE_REPLY
    summary = f"next: {route} ({reason})"
    if "plan" in update:
        summary = f"plan: {' -> '.join(plan or ['respond'])}; {summary}"
    return AgentOutcome(update=update, summary=summary)
