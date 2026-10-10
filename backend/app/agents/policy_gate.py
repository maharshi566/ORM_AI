"""Policy gate: which proposed actions need a person, and whose approval. Plain Python.

The investigation agent proposes actions; this gate decides, for each one:

* **who must approve** (``required_role``): the thresholds of POL-APPROVAL-001, worked
  out from the shop's own records (a purchase order's value at cost, a refund's bill
  total), so the model's guess about amounts plays no part;
* **whether it may run without asking** (low risk): only drafts and cases, only when
  the shopkeeper asked for an action, and only when nothing below raised a doubt;
* **whether it may never run** (``blocked``): an action justified only by an outside
  document, such as a supplier flyer (POL-AI-001 §3).

ORM_AI is stricter than POL-APPROVAL-001 §1 in one place: a payment reminder or a
message to a supplier always waits for a person, because a message cannot be taken
back.

Doubts about the whole request make every action wait for the owner:

* confidence under ``APPROVAL_CONFIDENCE`` (0.7);
* the message tried to change ORM_AI's rules (the input guardrail flagged it);
* the request asks for an exception to a rule ("just this once", "waive");
* a repeat claimant: the customer already returned goods recently.

When the investigation itself asks for human review, nothing runs without a person,
but a staff member may still decide what the matrix lets staff decide.

The gate only estimates. The tools check the approval again when they run, with the
real amounts, so an estimate that is too low can never let an action through.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.guardrails import injection_sentences
from app.models import Product, Sale, StockMovement
from app.tools.business_tools import (
    ADJUSTMENT_OWNER_THRESHOLD,
    PO_OWNER_THRESHOLD,
    REFUND_OWNER_THRESHOLD,
)

Role = Literal["staff", "owner"]

APPROVAL_CONFIDENCE = 0.7
REPEAT_CLAIM_DAYS = 30
REPEAT_CLAIM_LIMIT = 2  # returns in the window that make a customer a repeat claimant
LOW_RISK_TOOLS = frozenset({"create_case"})  # plus purchase-order drafts, see _required_role
EXCEPTION_WORDS = (
    "exception",
    "waive",
    "just this once",
    "even though",
    "override",
    "beyond the limit",
    "above the limit",
    "more than the limit",
    "ignore the rule",
    "bend the rule",
)


@dataclass
class ActionCheck:
    action_id: str
    required_role: Role | None  # None: the approval matrix needs nobody
    reasons: list[str] = field(default_factory=list)
    blocked: str | None = None  # why it may never run
    estimate: str | None = None  # e.g. "about Rs 1,400 at cost"


@dataclass
class GateResult:
    checks: dict[str, ActionCheck]
    triggers: list[str]  # doubts about the whole request (each makes the owner decide)
    review: list[str] = field(default_factory=list)  # a person decides; staff may

    def role_for(self, action_id: str) -> Role:
        """Who must approve this action, given the doubts about the whole request."""
        check = self.checks[action_id]
        if self.triggers or check.required_role == "owner":
            return "owner"
        return "staff"

    def runs_without_asking(self, action_id: str, *, wants_action: bool) -> bool:
        check = self.checks[action_id]
        return (
            check.blocked is None
            and check.required_role is None
            and not self.triggers
            and not self.review
            and wants_action
        )


def _money(value: Decimal) -> str:
    return f"Rs {value:,.2f}".replace(".00", "")


async def _products(session: AsyncSession, shop_id: str, ids: list[str]) -> dict[str, Product]:
    rows = await session.scalars(
        select(Product).where(Product.shop_id == shop_id, Product.id.in_(ids))
    )
    return {p.id: p for p in rows}


async def _recent_returns(session: AsyncSession, shop_id: str, customer_id: str, since: Any) -> int:
    returned = (
        select(func.count(func.distinct(StockMovement.reference_id)))
        .join(Sale, Sale.id == StockMovement.reference_id)
        .where(
            StockMovement.shop_id == shop_id,
            StockMovement.movement_type == "customer_return",
            StockMovement.moved_at >= since,
            Sale.customer_id == customer_id,
        )
    )
    return int(await session.scalar(returned) or 0)


async def _required_role(
    action: dict[str, Any], session: AsyncSession, state: dict[str, Any], now: Any
) -> ActionCheck:
    tool, args, shop_id = action["tool"], action["arguments"], state["shop_id"]
    check = ActionCheck(action_id=action["action_id"], required_role="staff")

    if tool == "create_purchase_order":
        if not args.get("submit_to_supplier"):
            check.required_role = None
            check.reasons.append("a draft for review; nothing is sent to the supplier")
            return check
        lines = args.get("lines") or []
        products = await _products(session, shop_id, [line["product_id"] for line in lines])
        if len(products) == len(lines):
            total = sum(
                (products[line["product_id"]].cost_price * line["quantity"] for line in lines),
                Decimal("0"),
            )
            check.estimate = f"about {_money(total)} at cost"
            if total > PO_OWNER_THRESHOLD:
                check.required_role = "owner"
                check.reasons.append(f"a purchase order over {_money(PO_OWNER_THRESHOLD)}")
                return check
        check.reasons.append(f"a purchase order up to {_money(PO_OWNER_THRESHOLD)}")
    elif tool == "record_stock_adjustment":
        products = await _products(session, shop_id, [args.get("product_id", "")])
        product = products.get(args.get("product_id", ""))
        if product is not None:
            value = product.cost_price * abs(int(args.get("quantity_change") or 0))
            check.estimate = f"about {_money(value)} at cost"
            if value > ADJUSTMENT_OWNER_THRESHOLD:
                check.required_role = "owner"
                check.reasons.append(
                    f"a stock adjustment over {_money(ADJUSTMENT_OWNER_THRESHOLD)} at cost"
                )
                return check
        check.reasons.append(
            f"a stock adjustment up to {_money(ADJUSTMENT_OWNER_THRESHOLD)} at cost"
        )
    elif tool == "update_selling_price":
        check.required_role = "owner"
        check.reasons.append("any change to a selling price")
    elif tool == "process_return":
        sale = await session.scalar(
            select(Sale).where(Sale.id == args.get("sale_id"), Sale.shop_id == shop_id)
        )
        if sale is not None:
            check.estimate = f"refund of about {_money(sale.total)}"
            if sale.customer_id:
                since = now - timedelta(days=REPEAT_CLAIM_DAYS)
                returns = await _recent_returns(session, shop_id, sale.customer_id, since)
                if returns >= REPEAT_CLAIM_LIMIT:
                    check.required_role = "owner"
                    check.reasons.append(
                        f"a repeat claimant: {sale.customer_id} returned goods {returns} "
                        f"times in the last {REPEAT_CLAIM_DAYS} days"
                    )
                    return check
            if sale.total > REFUND_OWNER_THRESHOLD:
                check.required_role = "owner"
                check.reasons.append(f"a refund over {_money(REFUND_OWNER_THRESHOLD)}")
                return check
        check.reasons.append(f"a refund up to {_money(REFUND_OWNER_THRESHOLD)}")
    elif tool == "send_payment_reminder":
        check.reasons.append("a message to a customer cannot be taken back")
    elif tool == "follow_up_supplier":
        check.reasons.append("a message to a supplier, POL-APPROVAL-001 §2")
    elif tool == "resolve_case":
        check.reasons.append("closing a case")
    elif tool in LOW_RISK_TOOLS:
        check.required_role = None
        check.reasons.append("only records a case for follow-up")
    else:
        check.required_role = "owner"
        check.reasons.append("an action the approval matrix does not list")
    return check


def _blocked_reason(action: dict[str, Any], state: dict[str, Any]) -> str | None:
    """An action justified only by an outside document may never run (POL-AI-001 §3)."""
    passages = state.get("retrieved_documents") or []
    untrusted = {p["citation"] for p in passages if p.get("trust") == "untrusted"}
    reason = action.get("reason") or ""
    cited = [c for c in untrusted if c in reason or c.strip("[]").split(" ")[0] in reason]
    trusted_cited = any(
        p["citation"] in reason or p["citation"].strip("[]").split(" ")[0] in reason
        for p in passages
        if p.get("trust") != "untrusted"
    )
    if cited and not trusted_cited:
        return "it rests only on an outside document, whose instructions ORM_AI never follows"
    text = f"{reason} {action.get('arguments')}".lower()
    for sentence in injection_sentences(passages):
        words = [w for w in sentence.lower().split() if len(w) > 3]
        if len(words) >= 4 and sum(w in text for w in words) >= max(4, len(words) // 2):
            return "it repeats an instruction found inside an outside document"
    return None


def request_triggers(state: dict[str, Any]) -> list[str]:
    triggers: list[str] = []
    confidence = state.get("confidence")
    if confidence is not None and confidence < APPROVAL_CONFIDENCE:
        triggers.append(f"low confidence ({confidence:.2f}, below {APPROVAL_CONFIDENCE})")
    flags = state.get("input_flags") or []
    if flags:
        triggers.append("the message tried to change ORM_AI's rules")
    query = (state.get("user_query") or "").lower()
    if any(word in query for word in EXCEPTION_WORDS):
        triggers.append("the request asks for an exception to a shop rule")
    return triggers


async def assess(
    actions: list[dict[str, Any]], state: dict[str, Any], session: AsyncSession, now: Any
) -> GateResult:
    checks: dict[str, ActionCheck] = {}
    for action in actions:
        check = await _required_role(action, session, state, now)
        check.blocked = _blocked_reason(action, state)
        checks[action["action_id"]] = check
    triggers = request_triggers(state)
    # A repeat claimant is a doubt about the request too: everything waits for the owner.
    for check in checks.values():
        if any(r.startswith("a repeat claimant") for r in check.reasons):
            triggers.append(next(r for r in check.reasons if r.startswith("a repeat claimant")))
    review = []
    if (state.get("investigation_result") or {}).get("requires_human_review"):
        review.append("the investigation asked for a person to review")
    return GateResult(checks=checks, triggers=list(dict.fromkeys(triggers)), review=review)
