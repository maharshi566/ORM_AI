"""Action tools: the only tools that change shop records. Only the Action agent gets them.

Each action tool follows the same steps:

1. **Idempotency.** Every call carries an ``idempotency_key``. If an earlier call
   with the same key already succeeded, the tool returns that result marked
   ``replayed: true`` instead of acting twice. This is what stops a retried or
   resumed workflow from placing two orders or sending two reminders.
2. **Validation and shop rules.** Bad input fails as ``invalid_input``; anything a
   shop policy forbids fails as ``policy_blocked`` with the reason.
3. **Approval.** Thresholds from the approval matrix (POL-APPROVAL-001) are checked
   in code. Without a matching ``ApprovalGrant`` the tool fails with
   ``approval_required`` and nothing changes.
4. **The change, plus an audit-log row**, in one transaction. The registry commits
   only if every step succeeded, so an action is never half done.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import func, select

from app.models import (
    Case,
    CreditEntry,
    Notification,
    PriceChange,
    Product,
    PurchaseOrder,
    PurchaseOrderItem,
    Shop,
    StockMovement,
)
from app.tools.api_tools import MockMessagingAPI, MockSupplierAPI
from app.tools.base import ToolContext, ToolError, ToolErrorCode, ToolInput, ToolOutput, ToolSpec
from app.tools.helpers import (
    audit,
    get_customer_row,
    get_product,
    get_sale_row,
    get_supplier_row,
    load_account,
    margin_percent,
    mask_phone,
    money,
    open_orders_for,
    to_ist,
)

# Thresholds from POL-APPROVAL-001 and the policies it points to.
PO_OWNER_THRESHOLD = Decimal("10000")
ADJUSTMENT_OWNER_THRESHOLD = Decimal("1000")
REFUND_OWNER_THRESHOLD = Decimal("2000")
RETURN_WINDOW_DAYS = 7
REMINDER_AFTER_DAYS_PAST_DUE = 7
REMINDER_GAP_DAYS = 7
REMINDER_MAX_UNANSWERED = 3
REMINDER_HOURS = (9, 20)  # 9:00 am to 8:00 pm
MIN_MARGIN_PERCENT = 8.0

IdempotencyKey = Field(
    min_length=8,
    max_length=80,
    pattern=r"^[A-Za-z0-9:._-]+$",
    description="Unique per intended action, e.g. '<workflow_id>:reorder:SUP-002'. "
    "Reusing a key returns the first result instead of acting twice.",
)


def blocked(message: str, **details: object) -> ToolError:
    return ToolError(ToolErrorCode.POLICY_BLOCKED, message, details=dict(details))


# ------------------------------------------------------------ purchase order


class OrderLine(ToolInput):
    product_id: str = Field(min_length=3, max_length=20)
    quantity: int = Field(ge=1, le=10000)


class CreatePurchaseOrderInput(ToolInput):
    supplier_id: str = Field(min_length=3, max_length=20)
    lines: list[OrderLine] = Field(min_length=1, max_length=50)
    submit_to_supplier: bool = Field(
        default=False,
        description="False saves a draft for review (no approval needed). True sends it to "
        "the supplier and needs approval: staff up to Rs 10,000, owner above.",
    )
    notes: str | None = Field(default=None, max_length=500)
    idempotency_key: str = IdempotencyKey

    @model_validator(mode="after")
    def unique_products(self) -> "CreatePurchaseOrderInput":
        ids = [line.product_id for line in self.lines]
        if len(ids) != len(set(ids)):
            raise ValueError("each product may appear only once")
        return self


class CreatedOrderLine(ToolOutput):
    product_id: str
    quantity: int
    unit_cost: Decimal


class CreatePurchaseOrderOutput(ToolOutput):
    purchase_order_id: str
    status: str
    supplier_id: str
    total_amount: Decimal
    expected_on: str | None
    supplier_ref: str | None
    items: list[CreatedOrderLine]
    replayed: bool = False


def _po_output(
    po: PurchaseOrder, supplier_ref: str | None, replayed: bool
) -> CreatePurchaseOrderOutput:
    return CreatePurchaseOrderOutput(
        purchase_order_id=po.id,
        status=str(po.status),
        supplier_id=po.supplier_id,
        total_amount=money(po.total_amount),
        expected_on=po.expected_on.isoformat() if po.expected_on else None,
        supplier_ref=supplier_ref,
        items=[
            CreatedOrderLine(
                product_id=i.product_id, quantity=i.quantity_ordered, unit_cost=money(i.unit_cost)
            )
            for i in po.items
        ],
        replayed=replayed,
    )


async def _next_id(ctx: ToolContext, column, prefix: str, width: int) -> str:  # type: ignore[no-untyped-def]
    latest = await ctx.session.scalar(select(func.max(column)).where(column.like(f"{prefix}-%")))
    number = int(latest.split("-")[-1]) + 1 if latest else 1
    return f"{prefix}-{number:0{width}d}"


async def create_purchase_order(
    ctx: ToolContext, args: CreatePurchaseOrderInput
) -> CreatePurchaseOrderOutput:
    existing = await ctx.session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.idempotency_key == args.idempotency_key)
    )
    if existing is not None:
        if existing.shop_id != ctx.shop_id:
            raise ToolError(ToolErrorCode.CONFLICT, "That idempotency key is already used.")
        return _po_output(existing, None, replayed=True)

    supplier = await get_supplier_row(ctx, args.supplier_id)
    if not supplier.is_active:
        raise blocked(f"{supplier.name} is not an active supplier.")
    products: list[Product] = [await get_product(ctx, line.product_id) for line in args.lines]
    inactive = [p.id for p in products if not p.is_active]
    if inactive:
        raise blocked("Inactive products cannot be reordered.", product_ids=inactive)

    open_orders = await open_orders_for(ctx, [p.id for p in products])
    if open_orders:
        raise ToolError(
            ToolErrorCode.CONFLICT,
            "Some products already have an open order. Follow up on it instead of ordering "
            "again (POL-REORDER-001).",
            details={
                "open_orders": {
                    pid: [o["purchase_order_id"] for o in lines]
                    for pid, lines in open_orders.items()
                }
            },
        )

    total = money(
        sum(p.cost_price * line.quantity for p, line in zip(products, args.lines, strict=True))
    )
    if args.submit_to_supplier:
        if total < supplier.min_order_value:
            raise blocked(
                f"{supplier.name} needs a minimum order of Rs {money(supplier.min_order_value)}; "
                f"this order is Rs {total}.",
                minimum=str(money(supplier.min_order_value)),
                total=str(total),
            )
        needed = "owner" if total > PO_OWNER_THRESHOLD else "staff"
        ctx.require_approval(needed, f"purchase order of Rs {total} to {supplier.name}")

    po = PurchaseOrder(
        id=await _next_id(ctx, PurchaseOrder.id, "PO", 5),
        shop_id=ctx.shop_id,
        supplier_id=supplier.id,
        status="placed" if args.submit_to_supplier else "draft",
        ordered_at=ctx.now if args.submit_to_supplier else None,
        expected_on=ctx.today + timedelta(days=supplier.lead_time_days)
        if args.submit_to_supplier
        else None,
        total_amount=total,
        notes=args.notes,
        created_by=ctx.actor,
        idempotency_key=args.idempotency_key,
        created_at=ctx.now,
    )
    po.items = [
        PurchaseOrderItem(
            product_id=p.id,
            quantity_ordered=line.quantity,
            quantity_received=0,
            unit_cost=money(p.cost_price),
        )
        for p, line in zip(products, args.lines, strict=True)
    ]
    ctx.session.add(po)
    await ctx.session.flush()

    supplier_ref = None
    if args.submit_to_supplier:
        api: MockSupplierAPI = ctx.clients["supplier_api"]
        supplier_ref = (await api.submit_order(po.id, supplier.id))["supplier_ref"]
    audit(
        ctx,
        "create_purchase_order",
        "purchase_order",
        po.id,
        {
            "status": str(po.status),
            "total": str(total),
            "supplier_ref": supplier_ref,
            "idempotency_key": args.idempotency_key,
        },
    )
    return _po_output(po, supplier_ref, replayed=False)


# ---------------------------------------------------------- stock adjustment


class StockAdjustmentInput(ToolInput):
    product_id: str = Field(min_length=3, max_length=20)
    quantity_change: int = Field(ge=-10000, le=10000, description="Negative removes stock")
    movement_type: Literal["adjustment", "damage"] = "adjustment"
    reason: str = Field(min_length=10, max_length=300)
    idempotency_key: str = IdempotencyKey

    @model_validator(mode="after")
    def not_zero(self) -> "StockAdjustmentInput":
        if self.quantity_change == 0:
            raise ValueError("quantity_change cannot be 0")
        return self


class StockAdjustmentOutput(ToolOutput):
    product_id: str
    quantity_change: int
    new_stock_qty: int
    value_at_cost: Decimal
    replayed: bool = False


async def record_stock_adjustment(
    ctx: ToolContext, args: StockAdjustmentInput
) -> StockAdjustmentOutput:
    product = await get_product(ctx, args.product_id)
    existing = await ctx.session.scalar(
        select(StockMovement).where(StockMovement.idempotency_key == args.idempotency_key)
    )
    value = money(abs(args.quantity_change) * product.cost_price)
    if existing is not None:
        return StockAdjustmentOutput(
            product_id=product.id,
            quantity_change=existing.quantity,
            new_stock_qty=product.stock_qty,
            value_at_cost=value,
            replayed=True,
        )
    if product.stock_qty + args.quantity_change < 0:
        raise ToolError(
            ToolErrorCode.INVALID_INPUT,
            f"Stock is {product.stock_qty}; it cannot go below zero.",
            details={"stock_qty": product.stock_qty},
        )
    needed = "owner" if value > ADJUSTMENT_OWNER_THRESHOLD else "staff"
    ctx.require_approval(needed, f"stock {args.movement_type} worth Rs {value} at cost")

    product.stock_qty += args.quantity_change
    ctx.session.add(
        StockMovement(
            shop_id=ctx.shop_id,
            product_id=product.id,
            movement_type=args.movement_type,
            quantity=args.quantity_change,
            reference_type="manual",
            reference_id=None,
            reason=args.reason,
            moved_at=ctx.now,
            created_by=ctx.actor,
            idempotency_key=args.idempotency_key,
        )
    )
    audit(
        ctx,
        "record_stock_adjustment",
        "product",
        product.id,
        {
            "quantity_change": args.quantity_change,
            "value_at_cost": str(value),
            "reason": args.reason,
        },
    )
    return StockAdjustmentOutput(
        product_id=product.id,
        quantity_change=args.quantity_change,
        new_stock_qty=product.stock_qty,
        value_at_cost=value,
    )


# ----------------------------------------------------------- payment reminder


class PaymentReminderInput(ToolInput):
    customer_id: str = Field(min_length=3, max_length=20)
    channel: Literal["whatsapp", "sms"] = "whatsapp"
    idempotency_key: str = IdempotencyKey


class PaymentReminderOutput(ToolOutput):
    notification_id: int
    customer_id: str
    sent_to: str | None = Field(description="Masked phone number")
    channel: str
    amount: Decimal
    message: str
    replayed: bool = False


async def send_payment_reminder(
    ctx: ToolContext, args: PaymentReminderInput
) -> PaymentReminderOutput:
    customer = await get_customer_row(ctx, args.customer_id)
    existing = await ctx.session.scalar(
        select(Notification).where(Notification.idempotency_key == args.idempotency_key)
    )
    if existing is not None:
        return PaymentReminderOutput(
            notification_id=existing.id,
            customer_id=customer.id,
            sent_to=mask_phone(existing.recipient),
            channel=str(existing.channel),
            amount=Decimal("0"),
            message=existing.message,
            replayed=True,
        )

    facts = await load_account(ctx, customer)
    # Each check below is a rule from POL-REMINDER-001.
    if facts.balance <= 0:
        raise blocked("The customer owes nothing.", balance=str(facts.balance))
    if facts.max_days_past_due < REMINDER_AFTER_DAYS_PAST_DUE:
        raise blocked(
            f"No bill is {REMINDER_AFTER_DAYS_PAST_DUE}+ days past due yet.",
            max_days_past_due=facts.max_days_past_due,
        )
    if facts.open_dispute_case_ids:
        raise blocked(
            "The customer has disputed the balance; reminders are paused.",
            case_ids=facts.open_dispute_case_ids,
        )
    last = facts.last_reminder_at
    if last is not None and (ctx.now - last) < timedelta(days=REMINDER_GAP_DAYS):
        raise blocked(
            f"A reminder was sent on {last:%d %b %Y}; wait {REMINDER_GAP_DAYS} days.",
            last_reminder_at=last.isoformat(),
        )
    since = facts.oldest_unpaid_since
    unanswered = [r for r in facts.reminders_sent if since and r.date() >= since]
    if len(unanswered) >= REMINDER_MAX_UNANSWERED:
        raise blocked(
            "Three reminders have gone unanswered; the owner should call the customer.",
            reminders_sent=len(unanswered),
        )
    paid = facts.last_payment
    if paid and (ctx.today - paid["date"]).days < 3:
        raise blocked("The customer paid in the last 3 days.", last_payment_date=str(paid["date"]))
    if not REMINDER_HOURS[0] <= ctx.now.hour < REMINDER_HOURS[1]:
        raise blocked(
            "Reminders are sent only between 9:00 am and 8:00 pm.", now=ctx.now.isoformat()
        )

    shop = await ctx.session.get(Shop, ctx.shop_id)
    message = (
        f"Namaste {customer.name}, a gentle reminder from {shop.name if shop else ctx.shop_id}: "
        f"Rs {facts.balance:,.2f} is pending on your account since {since:%d %b %Y}. "
        "Please pay when convenient. Thank you."
    )
    api: MockMessagingAPI = ctx.clients["messaging_api"]
    sent = await api.send(args.channel, customer.phone, message)
    notification = Notification(
        shop_id=ctx.shop_id,
        customer_id=customer.id,
        supplier_id=None,
        channel=args.channel,
        recipient=customer.phone,
        purpose="payment_reminder",
        message=message,
        status="sent",
        external_id=sent["message_id"],
        created_at=ctx.now,
        sent_at=ctx.now,
        created_by=ctx.actor,
        idempotency_key=args.idempotency_key,
    )
    ctx.session.add(notification)
    await ctx.session.flush()
    audit(
        ctx,
        "send_payment_reminder",
        "customer",
        customer.id,
        {"amount": str(facts.balance), "channel": args.channel, "message_id": sent["message_id"]},
    )
    return PaymentReminderOutput(
        notification_id=notification.id,
        customer_id=customer.id,
        sent_to=mask_phone(customer.phone),
        channel=args.channel,
        amount=facts.balance,
        message=message,
    )


# --------------------------------------------------------------- price change


class UpdatePriceInput(ToolInput):
    product_id: str = Field(min_length=3, max_length=20)
    new_selling_price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    reason: str = Field(min_length=10, max_length=300)
    idempotency_key: str = IdempotencyKey


class UpdatePriceOutput(ToolOutput):
    product_id: str
    old_price: Decimal
    new_price: Decimal
    cost_price: Decimal
    margin_percent: float
    warnings: list[str]
    replayed: bool = False


async def update_selling_price(ctx: ToolContext, args: UpdatePriceInput) -> UpdatePriceOutput:
    product = await get_product(ctx, args.product_id)
    existing = await ctx.session.scalar(
        select(PriceChange).where(PriceChange.idempotency_key == args.idempotency_key)
    )
    if existing is not None:
        return UpdatePriceOutput(
            product_id=product.id,
            old_price=money(existing.old_value),
            new_price=money(existing.new_value),
            cost_price=money(product.cost_price),
            margin_percent=margin_percent(existing.new_value, product.cost_price),
            warnings=[],
            replayed=True,
        )
    new_price = money(args.new_selling_price)
    cost = money(product.cost_price)
    if new_price > product.mrp:
        raise blocked(
            f"Rs {new_price} is above the MRP of Rs {money(product.mrp)}.",
            mrp=str(money(product.mrp)),
        )
    if new_price < cost:
        raise blocked(
            f"Rs {new_price} is below the cost price of Rs {cost}; only an approved "
            "clearance sale may do that (POL-PRICING-001).",
            cost_price=str(cost),
        )
    margin = margin_percent(new_price, cost)
    warnings = []
    if margin < MIN_MARGIN_PERCENT:
        warnings.append(f"Margin {margin}% is below the {MIN_MARGIN_PERCENT}% minimum.")
    ctx.require_approval("owner", f"price change for {product.name} to Rs {new_price}")

    old_price = money(product.selling_price)
    product.selling_price = new_price
    ctx.session.add(
        PriceChange(
            shop_id=ctx.shop_id,
            product_id=product.id,
            field="selling_price",
            old_value=old_price,
            new_value=new_price,
            reason=args.reason,
            changed_at=ctx.now,
            changed_by=ctx.actor,
            approved_by=ctx.approval.approved_by if ctx.approval else None,
            idempotency_key=args.idempotency_key,
        )
    )
    audit(
        ctx,
        "update_selling_price",
        "product",
        product.id,
        {"old": str(old_price), "new": str(new_price), "reason": args.reason},
    )
    return UpdatePriceOutput(
        product_id=product.id,
        old_price=old_price,
        new_price=new_price,
        cost_price=cost,
        margin_percent=margin,
        warnings=warnings,
    )


# --------------------------------------------------------------------- return


class ProcessReturnInput(ToolInput):
    sale_id: str = Field(min_length=3, max_length=20)
    reason: str = Field(min_length=5, max_length=300)
    idempotency_key: str = IdempotencyKey


class ProcessReturnOutput(ToolOutput):
    sale_id: str
    refund_amount: Decimal
    refund_method: str
    items_restocked: int
    replayed: bool = False


async def process_return(ctx: ToolContext, args: ProcessReturnInput) -> ProcessReturnOutput:
    sale = await get_sale_row(ctx, args.sale_id)
    first_key = f"{args.idempotency_key}:0"
    if await ctx.session.scalar(
        select(StockMovement.id).where(StockMovement.idempotency_key == first_key)
    ):
        return ProcessReturnOutput(
            sale_id=sale.id,
            refund_amount=money(sale.total),
            refund_method=_refund_method(str(sale.payment_mode)),
            items_restocked=len(sale.items),
            replayed=True,
        )
    if str(sale.status) != "completed":
        raise ToolError(ToolErrorCode.CONFLICT, f"{sale.id} is already {sale.status}.")
    sold_on = to_ist(sale.sold_at).date()  # type: ignore[union-attr]
    age = (ctx.today - sold_on).days
    if age > RETURN_WINDOW_DAYS:
        raise blocked(
            f"{sale.id} is {age} days old; returns are accepted within "
            f"{RETURN_WINDOW_DAYS} days (POL-RETURNS-001).",
            days_since_sale=age,
        )
    refund = money(sale.total)
    needed = "owner" if refund > REFUND_OWNER_THRESHOLD else "staff"
    ctx.require_approval(needed, f"refund of Rs {refund} for {sale.id}")

    sale.status = "returned"
    for index, item in enumerate(sale.items):
        product = await get_product(ctx, item.product_id)
        product.stock_qty += item.quantity
        ctx.session.add(
            StockMovement(
                shop_id=ctx.shop_id,
                product_id=item.product_id,
                movement_type="customer_return",
                quantity=item.quantity,
                reference_type="sale",
                reference_id=sale.id,
                reason=f"Customer return: {args.reason}",
                moved_at=ctx.now,
                created_by=ctx.actor,
                idempotency_key=f"{args.idempotency_key}:{index}",
            )
        )
    if str(sale.payment_mode) == "credit" and sale.customer_id:
        ctx.session.add(
            CreditEntry(
                shop_id=ctx.shop_id,
                customer_id=sale.customer_id,
                entry_type="adjustment",
                amount=-refund,
                sale_id=sale.id,
                entry_date=ctx.today,
                due_on=None,
                note=f"Return of {sale.id}: {args.reason}",
                created_by=ctx.actor,
                idempotency_key=args.idempotency_key,
            )
        )
    audit(ctx, "process_return", "sale", sale.id, {"refund": str(refund), "reason": args.reason})
    return ProcessReturnOutput(
        sale_id=sale.id,
        refund_amount=refund,
        refund_method=_refund_method(str(sale.payment_mode)),
        items_restocked=len(sale.items),
    )


def _refund_method(payment_mode: str) -> str:
    return "credit_balance_reduced" if payment_mode == "credit" else payment_mode


# ---------------------------------------------------------------------- cases


CaseCategoryName = Literal[
    "stock_discrepancy",
    "supplier_issue",
    "credit_dispute",
    "pricing",
    "customer_complaint",
    "reorder",
    "returns",
]


class CreateCaseInput(ToolInput):
    category: CaseCategoryName
    title: str = Field(min_length=5, max_length=200)
    description: str = Field(min_length=10, max_length=2000)
    customer_id: str | None = None
    product_id: str | None = None
    supplier_id: str | None = None


class CaseOutput(ToolOutput):
    case_id: str
    status: str
    replayed: bool = False


async def create_case(ctx: ToolContext, args: CreateCaseInput) -> CaseOutput:
    if args.customer_id:
        await get_customer_row(ctx, args.customer_id)
    if args.product_id:
        await get_product(ctx, args.product_id)
    if args.supplier_id:
        await get_supplier_row(ctx, args.supplier_id)
    duplicate = await ctx.session.scalar(
        select(Case).where(
            Case.shop_id == ctx.shop_id, Case.status == "open", Case.title == args.title
        )
    )
    if duplicate is not None:
        return CaseOutput(case_id=duplicate.id, status="open", replayed=True)
    case = Case(
        id=await _next_id(ctx, Case.id, "CASE", 4),
        shop_id=ctx.shop_id,
        category=args.category,
        title=args.title,
        description=args.description,
        status="open",
        resolution=None,
        customer_id=args.customer_id,
        supplier_id=args.supplier_id,
        product_id=args.product_id,
        opened_at=ctx.now,
        resolved_at=None,
    )
    ctx.session.add(case)
    audit(ctx, "create_case", "case", case.id, {"title": args.title})
    return CaseOutput(case_id=case.id, status="open")


class ResolveCaseInput(ToolInput):
    case_id: str = Field(min_length=3, max_length=20)
    resolution: str = Field(min_length=10, max_length=2000)


async def resolve_case(ctx: ToolContext, args: ResolveCaseInput) -> CaseOutput:
    case = await ctx.session.scalar(
        select(Case).where(Case.id == args.case_id, Case.shop_id == ctx.shop_id)
    )
    if case is None:
        raise ToolError(ToolErrorCode.NOT_FOUND, f"No case with ID {args.case_id} in this shop.")
    if str(case.status) == "resolved":
        return CaseOutput(case_id=case.id, status="resolved", replayed=True)
    ctx.require_approval("staff", f"closing case {case.id}")
    case.status = "resolved"
    case.resolution = args.resolution
    case.resolved_at = ctx.now
    audit(ctx, "resolve_case", "case", case.id, {"resolution": args.resolution})
    return CaseOutput(case_id=case.id, status="resolved")


def _action(name: str, description: str, handler, inp, out, timeout: float = 8.0) -> ToolSpec:  # type: ignore[no-untyped-def]
    return ToolSpec(
        name=name,
        description=description,
        input_model=inp,
        output_model=out,
        handler=handler,
        kind="action",
        timeout_seconds=timeout,
        max_retries=0,
    )


BUSINESS_TOOLS = [
    _action(
        "create_purchase_order",
        "Create a purchase order for one supplier. Saves a draft "
        "by default; sending it to the supplier needs approval. Fails if any product already "
        "has an open order.",
        create_purchase_order,
        CreatePurchaseOrderInput,
        CreatePurchaseOrderOutput,
    ),
    _action(
        "record_stock_adjustment",
        "Record a stock count correction or damaged/expired stock. "
        "Needs approval: staff up to Rs 1,000 at cost, owner above.",
        record_stock_adjustment,
        StockAdjustmentInput,
        StockAdjustmentOutput,
    ),
    _action(
        "send_payment_reminder",
        "Send a polite WhatsApp/SMS payment reminder. Refuses when "
        "the reminder policy forbids it (not overdue, reminded recently, disputed, outside hours).",
        send_payment_reminder,
        PaymentReminderInput,
        PaymentReminderOutput,
    ),
    _action(
        "update_selling_price",
        "Change a product's selling price. Must not exceed MRP or go "
        "below cost. Always needs the owner's approval.",
        update_selling_price,
        UpdatePriceInput,
        UpdatePriceOutput,
    ),
    _action(
        "process_return",
        "Return a whole bill within 7 days: restock items and refund. "
        "Needs approval: staff up to Rs 2,000, owner above.",
        process_return,
        ProcessReturnInput,
        ProcessReturnOutput,
    ),
    _action(
        "create_case",
        "Open a case to track a problem (no approval needed).",
        create_case,
        CreateCaseInput,
        CaseOutput,
    ),
    _action(
        "resolve_case",
        "Close a case with its resolution. Needs staff confirmation.",
        resolve_case,
        ResolveCaseInput,
        CaseOutput,
    ),
]
