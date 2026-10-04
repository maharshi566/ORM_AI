"""Queries and calculations shared by the read tools and the action tools.

Every lookup here filters by ``ctx.shop_id``. A record from another shop is
reported as "not found", exactly like a record that does not exist, so the tools
never reveal what other shops have.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select

from app.models import (
    AuditLog,
    Case,
    CreditEntry,
    Customer,
    Notification,
    Product,
    PurchaseOrder,
    PurchaseOrderItem,
    Sale,
    SaleItem,
    Supplier,
)
from app.tools.base import IST, ToolContext, ToolError, ToolErrorCode

OPEN_PO_STATUSES = ("placed", "partially_received")
CENT = Decimal("0.01")


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def margin_percent(selling_price: Any, cost_price: Any) -> float:
    cost = Decimal(str(cost_price))
    if cost == 0:
        return 0.0
    return float(((Decimal(str(selling_price)) - cost) / cost * 100).quantize(Decimal("0.1")))


def to_ist(value: datetime | None) -> datetime | None:
    """Database datetimes come back naive from SQLite and in UTC from PostgreSQL."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def ist_day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=IST)


def mask_phone(phone: str | None) -> str | None:
    """Show only the last three digits (POL-DATA-001)."""
    if not phone:
        return None
    head, tail = phone[:-3], phone[-3:]
    return "".join("x" if ch.isdigit() else ch for ch in head) + tail


def not_found(kind: str, record_id: str) -> ToolError:
    return ToolError(ToolErrorCode.NOT_FOUND, f"No {kind} with ID {record_id} in this shop.")


async def get_product(ctx: ToolContext, product_id: str) -> Product:
    product = await ctx.session.scalar(
        select(Product).where(Product.id == product_id, Product.shop_id == ctx.shop_id)
    )
    if product is None:
        raise not_found("product", product_id)
    return product


async def get_customer_row(ctx: ToolContext, customer_id: str) -> Customer:
    customer = await ctx.session.scalar(
        select(Customer).where(Customer.id == customer_id, Customer.shop_id == ctx.shop_id)
    )
    if customer is None:
        raise not_found("customer", customer_id)
    return customer


async def get_supplier_row(ctx: ToolContext, supplier_id: str) -> Supplier:
    # Suppliers are shared by every shop, so there is no shop filter here.
    supplier = await ctx.session.get(Supplier, supplier_id)
    if supplier is None:
        raise ToolError(ToolErrorCode.NOT_FOUND, f"No supplier with ID {supplier_id}.")
    return supplier


async def get_sale_row(ctx: ToolContext, sale_id: str) -> Sale:
    sale = await ctx.session.scalar(
        select(Sale).where(Sale.id == sale_id, Sale.shop_id == ctx.shop_id)
    )
    if sale is None:
        raise not_found("sale", sale_id)
    return sale


async def open_orders_for(ctx: ToolContext, product_ids: list[str]) -> dict[str, list[dict]]:
    """Open purchase-order lines (placed or partly received) for each product."""
    if not product_ids:
        return {}
    rows = await ctx.session.execute(
        select(PurchaseOrder, PurchaseOrderItem)
        .join(PurchaseOrderItem, PurchaseOrderItem.purchase_order_id == PurchaseOrder.id)
        .where(
            PurchaseOrder.shop_id == ctx.shop_id,
            PurchaseOrder.status.in_(OPEN_PO_STATUSES),
            PurchaseOrderItem.product_id.in_(product_ids),
        )
        .order_by(PurchaseOrder.id)
    )
    found: dict[str, list[dict]] = defaultdict(list)
    for po, item in rows:
        found[item.product_id].append(
            {
                "purchase_order_id": po.id,
                "status": str(po.status),
                "quantity_ordered": item.quantity_ordered,
                "quantity_received": item.quantity_received,
                "expected_on": po.expected_on,
                "days_late": days_late(po, ctx.today),
            }
        )
    return found


def days_late(po: PurchaseOrder, today: date) -> int:
    if str(po.status) != "placed" or po.expected_on is None:
        return 0
    return max(0, (today - po.expected_on).days)


async def sales_stats(ctx: ToolContext, product_ids: list[str], days: int) -> dict[str, dict]:
    """Units sold in the last ``days`` days and the last sale time, per product."""
    if not product_ids:
        return {}
    since = ist_day_start(ctx.today - timedelta(days=days - 1))
    rows = await ctx.session.execute(
        select(
            SaleItem.product_id,
            func.sum(SaleItem.quantity).filter(Sale.sold_at >= since),
            func.max(Sale.sold_at),
        )
        .join(Sale, Sale.id == SaleItem.sale_id)
        .where(SaleItem.product_id.in_(product_ids), Sale.status == "completed")
        .group_by(SaleItem.product_id)
    )
    stats = {pid: {"units": 0, "last_sale_at": None} for pid in product_ids}
    for pid, units, last in rows:
        stats[pid] = {"units": int(units or 0), "last_sale_at": to_ist(last)}
    return stats


@dataclass
class OpenBill:
    sale_id: str | None
    entry_date: date
    due_on: date | None
    amount_remaining: Decimal
    days_past_due: int


@dataclass
class AccountFacts:
    """A customer's credit position worked out from the ledger. Facts only, no policy."""

    balance: Decimal
    open_bills: list[OpenBill] = field(default_factory=list)
    last_payment: dict[str, Any] | None = None
    reminders_sent: list[datetime] = field(default_factory=list)
    open_dispute_case_ids: list[str] = field(default_factory=list)

    @property
    def max_days_past_due(self) -> int:
        return max((bill.days_past_due for bill in self.open_bills), default=0)

    @property
    def oldest_unpaid_since(self) -> date | None:
        return self.open_bills[0].entry_date if self.open_bills else None

    @property
    def last_reminder_at(self) -> datetime | None:
        return self.reminders_sent[-1] if self.reminders_sent else None


async def load_account(ctx: ToolContext, customer: Customer) -> AccountFacts:
    entries = (
        await ctx.session.scalars(
            select(CreditEntry)
            .where(CreditEntry.customer_id == customer.id)
            .order_by(CreditEntry.entry_date, CreditEntry.id)
        )
    ).all()

    # First in, first out: each payment clears the oldest unpaid bill first.
    queue: list[list[Any]] = []
    credit = Decimal("0")
    balance = Decimal("0")
    last_payment = None
    for entry in entries:
        amount = money(entry.amount)
        balance += amount
        if amount > 0:
            queue.append([entry.sale_id, entry.entry_date, entry.due_on, amount])
            continue
        if str(entry.entry_type) == "payment":
            last_payment = {"date": entry.entry_date, "amount": -amount, "note": entry.note}
        credit += -amount
        while credit > 0 and queue:
            take = min(credit, queue[0][3])
            queue[0][3] -= take
            credit -= take
            if queue[0][3] == 0:
                queue.pop(0)

    open_bills = [
        OpenBill(
            sale_id=sale_id,
            entry_date=entry_date,
            due_on=due_on,
            amount_remaining=money(remaining),
            days_past_due=max(0, (ctx.today - due_on).days) if due_on else 0,
        )
        for sale_id, entry_date, due_on, remaining in queue
    ]

    reminders = (
        await ctx.session.scalars(
            select(Notification.created_at)
            .where(
                Notification.customer_id == customer.id,
                Notification.purpose == "payment_reminder",
                Notification.status == "sent",
            )
            .order_by(Notification.created_at)
        )
    ).all()
    disputes = (
        await ctx.session.scalars(
            select(Case.id).where(
                Case.customer_id == customer.id,
                Case.category == "credit_dispute",
                Case.status == "open",
            )
        )
    ).all()
    return AccountFacts(
        balance=money(balance),
        open_bills=open_bills,
        last_payment=last_payment,
        reminders_sent=[to_ist(r) for r in reminders if r is not None],  # type: ignore[misc]
        open_dispute_case_ids=list(disputes),
    )


def audit(
    ctx: ToolContext, action: str, entity_type: str, entity_id: str, details: dict[str, Any]
) -> None:
    """Add an audit-log row to the current transaction (saved only if the action succeeds)."""
    actor_type = "user" if ctx.actor.startswith("USR-") else "agent"
    approval = ctx.approval
    ctx.session.add(
        AuditLog(
            actor_type=actor_type,
            actor_id=ctx.actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            shop_id=ctx.shop_id,
            workflow_id=ctx.workflow_id,
            details={
                **details,
                "approved_by": approval.approved_by if approval else None,
                "approval_id": approval.approval_id if approval else None,
            },
            created_at=ctx.now,
        )
    )
