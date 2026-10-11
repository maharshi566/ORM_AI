"""The morning briefing: what needs the owner's attention today (docs/briefing.md).

Every morning, for each shop: products at or below their reorder level, customers with
overdue credit, purchase orders that are late, approvals waiting, and yesterday's
sales. The figures come from the same read tools the agents use
(app/tools/database_tools.py), so the briefing and a chat answer always agree. No model
is involved: the briefing is plain counting, so it is free, instant and exact.

A briefing is stored once per shop and day (``briefings``), so the morning run can be
repeated safely: an existing briefing is reused, and one already sent on WhatsApp is
never sent again. The website shows today's briefing above the chat; with WhatsApp set
up, the owner also gets it as the ``daily_briefing`` template (docs/whatsapp.md).
"""

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.core.logging import get_logger
from app.core.phones import is_dialable, normalize_phone
from app.models import Approval, Briefing, CreditEntry, Customer, Shop, User
from app.tools.api_tools import MessagingAPI, UpstreamError
from app.tools.base import ToolContext
from app.tools.database_tools import (
    GetPurchaseOrdersInput,
    LowStockInput,
    SalesSummaryInput,
    get_low_stock_products,
    get_purchase_orders,
    get_sales_summary,
)
from app.tools.helpers import load_account, mask_phone

logger = get_logger(__name__)

LISTED = 5  # items named per section; the counts cover everything


@dataclass
class ShopBriefing:
    shop_id: str
    shop_name: str
    owner_name: str
    briefing_date: str  # YYYY-MM-DD
    low_stock: list[dict[str, Any]] = field(default_factory=list)
    low_stock_count: int = 0
    low_stock_not_ordered: int = 0
    overdue: list[dict[str, Any]] = field(default_factory=list)
    overdue_count: int = 0
    overdue_total: str = "0.00"
    late_orders: list[dict[str, Any]] = field(default_factory=list)
    approvals_waiting: int = 0
    yesterday_bills: int = 0
    yesterday_sales: str = "0.00"

    @property
    def first_name(self) -> str:
        return self.owner_name.split()[0] if self.owner_name else "there"

    def template_values(self) -> tuple[str, ...]:
        """The six values of the daily_briefing WhatsApp template."""
        return (
            self.first_name,
            self.shop_name,
            str(self.low_stock_count),
            str(self.overdue_count),
            str(len(self.late_orders)),
            str(self.approvals_waiting),
        )

    def text(self) -> str:
        day = date.fromisoformat(self.briefing_date)
        lines = [f"Good morning {self.first_name}. {self.shop_name}, {day:%A %d %b %Y}:"]
        if self.low_stock_count:
            names = "; ".join(
                f"{p['name']} ({p['product_id']}, {p['stock_qty']} left"
                + (", on order)" if p["on_order"] else ")")
                for p in self.low_stock
            )
            more = (
                f" and {self.low_stock_count - len(self.low_stock)} more"
                if self.low_stock_count > len(self.low_stock)
                else ""
            )
            lines.append(
                f"- Low on stock: {self.low_stock_count} products, {self.low_stock_not_ordered} "
                f"not yet ordered. {names}{more}."
            )
        else:
            lines.append("- Stock: nothing is below its reorder level.")
        if self.overdue_count:
            names = "; ".join(
                f"{c['name']} ({c['customer_id']}) Rs {Decimal(c['balance']):,.2f}, "
                f"{c['days_past_due']} days"
                for c in self.overdue
            )
            lines.append(
                f"- Overdue payments: {self.overdue_count} customers, "
                f"Rs {Decimal(self.overdue_total):,.2f} in all. {names}."
            )
        else:
            lines.append("- Credit: no payment is overdue.")
        if self.late_orders:
            names = "; ".join(
                f"{o['purchase_order_id']} from {o['supplier_name']}, {o['days_late']} days late"
                for o in self.late_orders
            )
            lines.append(f"- Late deliveries: {names}.")
        if self.approvals_waiting:
            lines.append(f"- Waiting for approval: {self.approvals_waiting}.")
        if self.yesterday_bills:
            lines.append(
                f"- Yesterday: {self.yesterday_bills} bills, "
                f"Rs {Decimal(self.yesterday_sales):,.2f}."
            )
        else:
            lines.append("- Yesterday: no sales recorded.")
        return "\n".join(lines)


async def _overdue_customers(ctx: ToolContext) -> list[dict[str, Any]]:
    """Customers whose oldest unpaid bill is past due, biggest balance first."""
    owing = (
        await ctx.session.scalars(
            select(CreditEntry.customer_id)
            .where(CreditEntry.shop_id == ctx.shop_id)
            .group_by(CreditEntry.customer_id)
            .having(func.sum(CreditEntry.amount) > 0)
        )
    ).all()
    found = []
    for customer_id in owing:
        customer = await ctx.session.get(Customer, customer_id)
        if customer is None:
            continue
        facts = await load_account(ctx, customer)
        if facts.balance > 0 and facts.max_days_past_due > 0:
            found.append(
                {
                    "customer_id": customer.id,
                    "name": customer.name,
                    "balance": str(facts.balance),
                    "days_past_due": facts.max_days_past_due,
                }
            )
    return sorted(found, key=lambda c: (-Decimal(c["balance"]), c["customer_id"]))


async def build_briefing(session: AsyncSession, shop_id: str, now: datetime) -> ShopBriefing:
    shop = await session.get(Shop, shop_id)
    owner = await session.scalar(
        select(User).where(User.shop_id == shop_id, User.role == "owner", User.is_active.is_(True))
    )
    ctx = ToolContext(session=session, shop_id=shop_id, actor="briefing", now=now)
    low = await get_low_stock_products(ctx, LowStockInput(include_already_ordered=True))
    orders = await get_purchase_orders(ctx, GetPurchaseOrdersInput(limit=50))
    yesterday = ctx.today - timedelta(days=1)
    sales = await get_sales_summary(
        ctx, SalesSummaryInput(start_date=yesterday, end_date=yesterday)
    )
    overdue = await _overdue_customers(ctx)
    waiting = await session.scalar(
        select(func.count()).where(Approval.shop_id == shop_id, Approval.status == "pending")
    )
    late = sorted((o for o in orders.orders if o.days_late > 0), key=lambda o: -o.days_late)
    return ShopBriefing(
        shop_id=shop_id,
        shop_name=shop.name if shop else shop_id,
        owner_name=owner.name if owner else "",
        briefing_date=ctx.today.isoformat(),
        low_stock=[
            {
                "product_id": item.product_id,
                "name": item.name,
                "stock_qty": item.stock_qty,
                "reorder_level": item.reorder_level,
                "on_order": bool(item.open_orders),
            }
            for item in low.items[:LISTED]
        ],
        low_stock_count=len(low.items),
        low_stock_not_ordered=low.without_open_order,
        overdue=overdue[:LISTED],
        overdue_count=len(overdue),
        overdue_total=str(sum((Decimal(c["balance"]) for c in overdue), Decimal("0.00"))),
        late_orders=[
            {
                "purchase_order_id": o.purchase_order_id,
                "supplier_name": o.supplier_name,
                "days_late": o.days_late,
            }
            for o in late[:LISTED]
        ],
        approvals_waiting=int(waiting or 0),
        yesterday_bills=sales.bills,
        yesterday_sales=str(sales.total_sales),
    )


async def todays_briefing(
    factory: async_sessionmaker[AsyncSession], shop_id: str, now: datetime
) -> Briefing:
    """Today's stored briefing for the shop, built (and stored) when there is none yet."""
    async with factory() as session:
        stored = await session.scalar(
            select(Briefing).where(
                Briefing.shop_id == shop_id, Briefing.briefing_date == now.date()
            )
        )
        if stored is not None:
            return stored
        briefing = await build_briefing(session, shop_id, now)
        row = Briefing(
            shop_id=shop_id,
            briefing_date=now.date(),
            content=asdict(briefing),
            text=briefing.text(),
            created_at=now,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:  # built at the same moment by another request: use that one
            await session.rollback()
            row = await session.scalar(
                select(Briefing).where(
                    Briefing.shop_id == shop_id, Briefing.briefing_date == now.date()
                )
            )
        return row  # type: ignore[return-value]


def _send_to(owner: User | None, shop_id: str, settings: Settings) -> str | None:
    """The owner's number, when this shop gets the WhatsApp briefing."""
    if owner is None:
        return None
    number = normalize_phone(owner.phone_e164 or owner.phone)
    listed = settings.briefing_shop_list
    if listed:
        return number if shop_id in listed and number else None
    return number if is_dialable(number) else None


@dataclass
class RunResult:
    built: int = 0
    sent: int = 0
    not_sent: int = 0
    failed: list[str] = field(default_factory=list)


async def run_briefings(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    messaging: MessagingAPI | None,
    now: datetime,
    *,
    shop_ids: list[str] | None = None,
) -> RunResult:
    """Build today's briefing for every active shop (or ``shop_ids``) and send it to the
    owner on WhatsApp where that applies. Safe to run twice."""
    result = RunResult()
    async with factory() as session:
        query = select(Shop.id).order_by(Shop.id)
        if shop_ids:
            query = query.where(Shop.id.in_(shop_ids))
        shops = list((await session.scalars(query)).all())
    for shop_id in shops:
        try:
            row = await todays_briefing(factory, shop_id, now)
        except Exception as exc:  # one broken shop must not stop the others
            logger.exception("briefing failed", shop_id=shop_id)
            result.failed.append(f"{shop_id}: {type(exc).__name__}")
            continue
        result.built += 1
        if messaging is None or row.sent_at is not None:
            continue
        async with factory() as session:
            row = await session.get(Briefing, row.id)
            owner = await session.scalar(
                select(User).where(
                    User.shop_id == shop_id, User.role == "owner", User.is_active.is_(True)
                )
            )
            number = _send_to(owner, shop_id, settings)
            if row is None or number is None:
                result.not_sent += 1
                continue
            values = ShopBriefing(**row.content).template_values()
            try:
                sent = await messaging.send(
                    "whatsapp", number, row.text, purpose="daily_briefing", params=values
                )
            except UpstreamError as exc:
                row.send_error = str(exc)
                result.failed.append(f"{shop_id}: {exc}")
            else:
                row.sent_at = now
                row.sent_to = mask_phone(number)
                row.message_id = sent["message_id"]
                row.send_error = None
                result.sent += 1
            await session.commit()
    return result
