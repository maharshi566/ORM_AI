"""Action agent: carries out the actions a person approved. No LLM.

It runs right after human review, and only for actions whose status is ``approved``
(by a person, or by the policy gate for a low-risk draft the shopkeeper asked for) or
``modified`` (approved with changed details). For each one it:

1. builds an ``ApprovalGrant`` from the recorded decision (who approved, their role,
   the approval ID). This is the only place a grant is ever made, and only from a
   decision that came through the approval API;
2. calls the action tool through the ToolRegistry, in its own database session, so a
   failing action cannot undo another one;
3. records the tool's real result. An action is ``done`` only when its tool returned
   success; anything else is ``failed``, with the tool's own reason (for example "a
   reminder was sent 2 days ago" or "this needs the owner's approval").

Every action carries an idempotency key tied to the workflow, so running this node
again (a retry after a crash) replays the first result instead of acting twice.
No model is involved: there is nothing to decide here, only to do and report.
"""

from typing import Any

from app.agents.common import AgentOutcome, compact
from app.graph.deps import AgentDeps
from app.tools.base import ApprovalGrant

AGENT = "action"
RUNNABLE = frozenset({"approved", "modified"})


def _rs(value: Any) -> str:
    try:
        return f"Rs {float(value):,.2f}".replace(".00", "")
    except (TypeError, ValueError):
        return f"Rs {value}"


def summarize(tool: str, data: dict[str, Any]) -> str:
    """The tool's result in one plain sentence for the shopkeeper (IDs kept)."""
    d = data
    if tool == "follow_up_supplier":
        text = (
            f"Message sent to {d.get('supplier_name')} on {d.get('channel')} "
            f"about {d.get('purchase_order_id')}."
        )
        if d.get("next_step"):
            text += f" Next: {d['next_step']}"
    elif tool == "send_payment_reminder":
        text = (
            f"Reminder about {_rs(d.get('amount'))} sent to {d.get('customer_id')} on "
            f"{d.get('channel')} ({d.get('sent_to')})."
        )
    elif tool == "create_purchase_order":
        text = (
            f"{d.get('purchase_order_id')} saved as {d.get('status')}: "
            f"{_rs(d.get('total_amount'))} to {d.get('supplier_id')}"
            + (f", expected on {d['expected_on']}." if d.get("expected_on") else ".")
        )
    elif tool == "record_stock_adjustment":
        text = (
            f"{d.get('product_id')} changed by {d.get('quantity_change')}; stock is now "
            f"{d.get('new_stock_qty')} ({_rs(d.get('value_at_cost'))} at cost)."
        )
    elif tool == "update_selling_price":
        text = (
            f"{d.get('product_id')} price changed from {_rs(d.get('old_price'))} to "
            f"{_rs(d.get('new_price'))} (margin {d.get('margin_percent')}%)."
        )
        if d.get("warnings"):
            text += " " + " ".join(d["warnings"])
    elif tool == "process_return":
        text = (
            f"{d.get('sale_id')} returned: {_rs(d.get('refund_amount'))} refunded by "
            f"{d.get('refund_method')}, {d.get('items_restocked')} item(s) back in stock."
        )
    elif tool in {"create_case", "resolve_case"}:
        text = f"{d.get('case_id')} is {d.get('status')}."
    else:
        text = compact(d, 300)
    if d.get("replayed"):
        text += " (This was already done earlier, so it was not repeated.)"
    return text


def _grant(action: dict[str, Any]) -> ApprovalGrant | None:
    approved_by = action.get("approved_by")
    if not approved_by or approved_by == "policy":
        return None  # low risk: the tool itself needs no approval
    role = "owner" if action.get("approver_role") == "owner" else "staff"
    return ApprovalGrant(approved_by=approved_by, role=role, approval_id=action.get("approval_id"))


async def run(state: dict[str, Any], deps: AgentDeps) -> AgentOutcome:
    actions = [dict(a) for a in state.get("proposed_actions") or []]
    results: list[dict[str, Any]] = []
    log: list[dict[str, Any]] = []
    for action in actions:
        if action.get("status") not in RUNNABLE:
            continue
        async with deps.session_factory() as session:
            result = await deps.call_tool(
                session, state, AGENT, action["tool"], action["arguments"], approval=_grant(action)
            )
        log.append(
            {
                "agent": AGENT,
                "tool": action["tool"],
                "arguments": action["arguments"],
                "status": result.status,
                "error_code": str(result.error_code) if result.error_code else None,
                "latency_ms": result.latency_ms,
            }
        )
        results.append(
            {
                "action_id": action["action_id"],
                "tool": action["tool"],
                "status": result.status,
                "data": result.data,
                "error_code": str(result.error_code) if result.error_code else None,
                "error_message": result.error_message,
            }
        )
        if result.ok:
            action["status"] = "done"
            action["result"] = summarize(action["tool"], result.data or {})
        else:
            action["status"] = "failed"
            action["error_code"] = str(result.error_code)
            action["result"] = result.error_message or "The tool failed."
    done = sum(r["status"] == "success" for r in results)
    return AgentOutcome(
        update={
            "proposed_actions": actions,
            "action_results": results,
            "tool_results": log,
            "route": "respond",
            "completed_steps": [AGENT],
        },
        summary=f"{done} done, {len(results) - done} failed"
        + (
            f" ({', '.join(r['tool'] for r in results if r['status'] != 'success')})"
            if done < len(results)
            else ""
        ),
    )
