"""Read-only tools over the shop's own records. Used by the Data retrieval agent.

They return facts only: balances, days overdue, stock against reorder levels.
Deciding what those facts mean under a policy is the Investigation agent's job.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import func, or_, select

from app.models import (
    Case,
    CreditEntry,
    Customer,
    Product,
    PurchaseOrder,
    Sale,
    SaleItem,
    StockMovement,
    Supplier,
)
from app.tools.base import ToolContext, ToolInput, ToolOutput, ToolSpec
from app.tools.helpers import (
    days_late,
    get_customer_row,
    get_product,
    get_sale_row,
    get_supplier_row,
    ist_day_start,
    load_account,
    margin_percent,
    mask_phone,
    money,
    open_orders_for,
    sales_stats,
    to_ist,
)

ID = Field(min_length=3, max_length=20)

# ------------------------------------------------------------------ products


class ProductSummary(ToolOutput):
    product_id: str
    sku: str
    name: str
    category: str
    unit: str
    stock_qty: int
    reorder_level: int
    selling_price: Decimal
    mrp: Decimal
    is_active: bool


def _summary(p: Product) -> ProductSummary:
    return ProductSummary(
        product_id=p.id,
        sku=p.sku,
        name=p.name,
        category=p.category,
        unit=p.unit,
        stock_qty=p.stock_qty,
        reorder_level=p.reorder_level,
        selling_price=money(p.selling_price),
        mrp=money(p.mrp),
        is_active=p.is_active,
    )


class SearchProductsInput(ToolInput):
    query: str | None = Field(default=None, max_length=60, description="Part of the product name")
    category: str | None = Field(default=None, max_length=40)
    include_inactive: bool = False
    limit: int = Field(default=20, ge=1, le=50)


class SearchProductsOutput(ToolOutput):
    products: list[ProductSummary]
    total_matches: int


async def search_products(ctx: ToolContext, args: SearchProductsInput) -> SearchProductsOutput:
    conditions = [Product.shop_id == ctx.shop_id]
    if args.query:
        conditions.append(Product.name.icontains(args.query, autoescape=True))
    if args.category:
        conditions.append(Product.category == args.category.lower())
    if not args.include_inactive:
        conditions.append(Product.is_active.is_(True))
    total = await ctx.session.scalar(select(func.count()).select_from(Product).where(*conditions))
    rows = await ctx.session.scalars(
        select(Product).where(*conditions).order_by(Product.name).limit(args.limit)
    )
    return SearchProductsOutput(products=[_summary(p) for p in rows], total_matches=total or 0)


class OpenOrderLine(ToolOutput):
    purchase_order_id: str
    status: str
    quantity_ordered: int
    quantity_received: int
    expected_on: date | None
    days_late: int


class GetProductInput(ToolInput):
    product_id: str = ID


class GetProductOutput(ProductSummary):
    cost_price: Decimal
    margin_percent: float = Field(description="(selling - cost) / cost, in percent")
    gst_rate: Decimal
    reorder_qty: int
    preferred_supplier_id: str | None
    preferred_supplier_name: str | None
    supplier_lead_time_days: int | None
    open_orders: list[OpenOrderLine]
    units_sold_last_30_days: int
    last_sale_at: datetime | None


async def get_product_details(ctx: ToolContext, args: GetProductInput) -> GetProductOutput:
    p = await get_product(ctx, args.product_id)
    supplier = (
        await ctx.session.get(Supplier, p.preferred_supplier_id)
        if p.preferred_supplier_id
        else None
    )
    orders = (await open_orders_for(ctx, [p.id])).get(p.id, [])
    stats = (await sales_stats(ctx, [p.id], 30))[p.id]
    return GetProductOutput(
        **_summary(p).model_dump(),
        cost_price=money(p.cost_price),
        margin_percent=margin_percent(p.selling_price, p.cost_price),
        gst_rate=money(p.gst_rate),
        reorder_qty=p.reorder_qty,
        preferred_supplier_id=p.preferred_supplier_id,
        preferred_supplier_name=supplier.name if supplier else None,
        supplier_lead_time_days=supplier.lead_time_days if supplier else None,
        open_orders=[OpenOrderLine(**o) for o in orders],
        units_sold_last_30_days=stats["units"],
        last_sale_at=stats["last_sale_at"],
    )


class LowStockInput(ToolInput):
    include_already_ordered: bool = Field(
        default=True, description="Also list low items that already have an open order"
    )


class LowStockItem(ToolOutput):
    product_id: str
    name: str
    stock_qty: int
    reorder_level: int
    reorder_qty: int
    cost_price: Decimal
    preferred_supplier_id: str | None
    preferred_supplier_name: str | None
    open_orders: list[OpenOrderLine]
    units_sold_last_30_days: int


class LowStockOutput(ToolOutput):
    as_of: date
    items: list[LowStockItem]
    without_open_order: int
    with_open_order: int


async def get_low_stock_products(ctx: ToolContext, args: LowStockInput) -> LowStockOutput:
    rows = (
        await ctx.session.execute(
            select(Product, Supplier.name)
            .outerjoin(Supplier, Supplier.id == Product.preferred_supplier_id)
            .where(
                Product.shop_id == ctx.shop_id,
                Product.is_active.is_(True),
                Product.stock_qty <= Product.reorder_level,
            )
            .order_by(Product.stock_qty - Product.reorder_level, Product.id)
        )
    ).all()
    ids = [p.id for p, _ in rows]
    orders = await open_orders_for(ctx, ids)
    stats = await sales_stats(ctx, ids, 30)
    items = []
    for p, supplier_name in rows:
        if orders.get(p.id) and not args.include_already_ordered:
            continue
        items.append(
            LowStockItem(
                product_id=p.id,
                name=p.name,
                stock_qty=p.stock_qty,
                reorder_level=p.reorder_level,
                reorder_qty=p.reorder_qty,
                cost_price=money(p.cost_price),
                preferred_supplier_id=p.preferred_supplier_id,
                preferred_supplier_name=supplier_name,
                open_orders=[OpenOrderLine(**o) for o in orders.get(p.id, [])],
                units_sold_last_30_days=stats[p.id]["units"],
            )
        )
    with_order = sum(1 for pid in ids if orders.get(pid))
    return LowStockOutput(
        as_of=ctx.today,
        items=items,
        without_open_order=len(ids) - with_order,
        with_open_order=with_order,
    )


class SlowMovingInput(ToolInput):
    days: int = Field(default=90, ge=14, le=365, description="Look-back window in days")


class SlowMovingItem(ToolOutput):
    product_id: str
    name: str
    stock_qty: int
    stock_value_at_cost: Decimal
    is_active: bool
    last_sale_at: datetime | None


class SlowMovingOutput(ToolOutput):
    days: int
    items: list[SlowMovingItem]


async def get_slow_moving_products(ctx: ToolContext, args: SlowMovingInput) -> SlowMovingOutput:
    products = (
        await ctx.session.scalars(
            select(Product)
            .where(Product.shop_id == ctx.shop_id, Product.stock_qty > 0)
            .order_by(Product.id)
        )
    ).all()
    stats = await sales_stats(ctx, [p.id for p in products], args.days)
    items = [
        SlowMovingItem(
            product_id=p.id,
            name=p.name,
            stock_qty=p.stock_qty,
            stock_value_at_cost=money(p.cost_price * p.stock_qty),
            is_active=p.is_active,
            last_sale_at=stats[p.id]["last_sale_at"],
        )
        for p in products
        if stats[p.id]["units"] == 0
    ]
    return SlowMovingOutput(days=args.days, items=items)


class StockMovementsInput(ToolInput):
    product_id: str = ID
    days: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=50, ge=1, le=200)


class MovementRow(ToolOutput):
    moved_at: datetime
    movement_type: str
    quantity: int
    reference: str | None
    reason: str | None
    created_by: str


class StockMovementsOutput(ToolOutput):
    product_id: str
    name: str
    current_stock: int
    movements: list[MovementRow] = Field(description="Newest first")
    totals_by_type: dict[str, int]


async def get_stock_movements(ctx: ToolContext, args: StockMovementsInput) -> StockMovementsOutput:
    product = await get_product(ctx, args.product_id)
    since = ist_day_start(ctx.today - timedelta(days=args.days - 1))
    rows = (
        await ctx.session.scalars(
            select(StockMovement)
            .where(StockMovement.product_id == product.id, StockMovement.moved_at >= since)
            .order_by(StockMovement.moved_at.desc(), StockMovement.id.desc())
        )
    ).all()
    totals: dict[str, int] = defaultdict(int)
    for m in rows:
        totals[str(m.movement_type)] += m.quantity
    return StockMovementsOutput(
        product_id=product.id,
        name=product.name,
        current_stock=product.stock_qty,
        movements=[
            MovementRow(
                moved_at=to_ist(m.moved_at),  # type: ignore[arg-type]
                movement_type=str(m.movement_type),
                quantity=m.quantity,
                reference=f"{m.reference_type}:{m.reference_id}" if m.reference_id else None,
                reason=m.reason,
                created_by=m.created_by,
            )
            for m in rows[: args.limit]
        ],
        totals_by_type=dict(totals),
    )


# --------------------------------------------------------------------- sales


class SalesSummaryInput(ToolInput):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def check_range(self) -> "SalesSummaryInput":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if (self.end_date - self.start_date).days > 366:
            raise ValueError("the range can be at most one year")
        return self


class TopProduct(ToolOutput):
    product_id: str
    name: str
    units: int
    revenue: Decimal


class SalesSummaryOutput(ToolOutput):
    start_date: date
    end_date: date
    bills: int
    returned_bills: int
    total_sales: Decimal = Field(description="Completed bills, after discounts, GST included")
    total_discount: Decimal
    gst_included: Decimal
    by_payment_mode: dict[str, Decimal]
    top_products: list[TopProduct]


async def get_sales_summary(ctx: ToolContext, args: SalesSummaryInput) -> SalesSummaryOutput:
    start = ist_day_start(args.start_date)
    end = ist_day_start(args.end_date + timedelta(days=1))
    in_range = [Sale.shop_id == ctx.shop_id, Sale.sold_at >= start, Sale.sold_at < end]
    rows = (
        await ctx.session.execute(
            select(
                Sale.status,
                Sale.payment_mode,
                func.count(),
                func.sum(Sale.total),
                func.sum(Sale.discount),
                func.sum(Sale.tax_amount),
            )
            .where(*in_range)
            .group_by(Sale.status, Sale.payment_mode)
        )
    ).all()
    bills = returned = 0
    total = discount = tax = Decimal("0")
    by_mode: dict[str, Decimal] = {}
    for status, mode, count, amount, disc, gst in rows:
        if str(status) != "completed":
            returned += count if str(status) == "returned" else 0
            continue
        bills += count
        total += money(amount)
        discount += money(disc)
        tax += money(gst)
        by_mode[str(mode)] = by_mode.get(str(mode), Decimal("0")) + money(amount)
    top = (
        await ctx.session.execute(
            select(
                Product.id, Product.name, func.sum(SaleItem.quantity), func.sum(SaleItem.line_total)
            )
            .join(SaleItem, SaleItem.product_id == Product.id)
            .join(Sale, Sale.id == SaleItem.sale_id)
            .where(*in_range, Sale.status == "completed")
            .group_by(Product.id, Product.name)
            .order_by(func.sum(SaleItem.line_total).desc(), Product.id)
            .limit(5)
        )
    ).all()
    return SalesSummaryOutput(
        start_date=args.start_date,
        end_date=args.end_date,
        bills=bills,
        returned_bills=returned,
        total_sales=money(total),
        total_discount=money(discount),
        gst_included=money(tax),
        by_payment_mode={k: money(v) for k, v in sorted(by_mode.items())},
        top_products=[
            TopProduct(product_id=pid, name=name, units=int(units), revenue=money(revenue))
            for pid, name, units, revenue in top
        ],
    )


class GetSaleInput(ToolInput):
    sale_id: str = ID


class SaleLine(ToolOutput):
    product_id: str
    name: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal


class GetSaleOutput(ToolOutput):
    sale_id: str
    sold_at: datetime
    status: str
    payment_mode: str
    customer_id: str | None
    customer_name: str | None
    subtotal: Decimal
    discount: Decimal
    total: Decimal
    items: list[SaleLine]
    days_since_sale: int


async def get_sale(ctx: ToolContext, args: GetSaleInput) -> GetSaleOutput:
    sale = await get_sale_row(ctx, args.sale_id)
    names = dict(
        (
            await ctx.session.execute(
                select(Product.id, Product.name).where(
                    Product.id.in_([i.product_id for i in sale.items])
                )
            )
        ).all()
    )
    customer = await ctx.session.get(Customer, sale.customer_id) if sale.customer_id else None
    sold_at = to_ist(sale.sold_at)
    assert sold_at is not None  # noqa: S101
    return GetSaleOutput(
        sale_id=sale.id,
        sold_at=sold_at,
        status=str(sale.status),
        payment_mode=str(sale.payment_mode),
        customer_id=sale.customer_id,
        customer_name=customer.name if customer else None,
        subtotal=money(sale.subtotal),
        discount=money(sale.discount),
        total=money(sale.total),
        items=[
            SaleLine(
                product_id=i.product_id,
                name=names.get(i.product_id, i.product_id),
                quantity=i.quantity,
                unit_price=money(i.unit_price),
                line_total=money(i.line_total),
            )
            for i in sale.items
        ],
        days_since_sale=(ctx.today - sold_at.date()).days,
    )


# ----------------------------------------------------------------- customers


class SearchCustomersInput(ToolInput):
    query: str = Field(min_length=2, max_length=60, description="Part of a name or phone number")
    limit: int = Field(default=10, ge=1, le=25)


class CustomerSummary(ToolOutput):
    customer_id: str
    name: str
    phone: str | None = Field(description="Masked: only the last 3 digits are shown")
    locality: str
    customer_type: str
    credit_allowed: bool
    credit_limit: Decimal
    is_active: bool


def _customer_summary(c: Customer) -> CustomerSummary:
    return CustomerSummary(
        customer_id=c.id,
        name=c.name,
        phone=mask_phone(c.phone),
        locality=c.locality,
        customer_type=c.customer_type,
        credit_allowed=c.credit_allowed,
        credit_limit=money(c.credit_limit),
        is_active=c.is_active,
    )


class SearchCustomersOutput(ToolOutput):
    customers: list[CustomerSummary]


async def search_customers(ctx: ToolContext, args: SearchCustomersInput) -> SearchCustomersOutput:
    digits = "".join(ch for ch in args.query if ch.isdigit())
    match = [Customer.name.icontains(args.query, autoescape=True)]
    if len(digits) >= 3:
        match.append(Customer.phone.contains(digits, autoescape=True))
    rows = await ctx.session.scalars(
        select(Customer)
        .where(Customer.shop_id == ctx.shop_id, or_(*match))
        .order_by(Customer.name, Customer.id)
        .limit(args.limit)
    )
    return SearchCustomersOutput(customers=[_customer_summary(c) for c in rows])


class GetCustomerAccountInput(ToolInput):
    customer_id: str = ID


class OpenBillRow(ToolOutput):
    sale_id: str | None
    bought_on: date
    due_on: date | None
    amount_remaining: Decimal
    days_past_due: int


class LedgerRow(ToolOutput):
    entry_date: date
    entry_type: str
    amount: Decimal = Field(description="Positive adds to what the customer owes")
    note: str | None


class GetCustomerAccountOutput(CustomerSummary):
    joined_on: date
    notes: str | None
    balance: Decimal = Field(description="What the customer owes now; negative means overpaid")
    available_credit: Decimal
    over_limit_by: Decimal
    open_bills: list[OpenBillRow] = Field(description="Unpaid bills, oldest first (FIFO)")
    max_days_past_due: int
    last_payment: dict | None
    reminders_sent_total: int
    last_reminder_at: datetime | None
    open_dispute_case_ids: list[str]
    recent_entries: list[LedgerRow] = Field(description="Last 10 ledger entries, newest first")


async def get_customer_account(
    ctx: ToolContext, args: GetCustomerAccountInput
) -> GetCustomerAccountOutput:
    customer = await get_customer_row(ctx, args.customer_id)
    facts = await load_account(ctx, customer)
    limit = money(customer.credit_limit)
    recent = (
        await ctx.session.scalars(
            select(CreditEntry)
            .where(CreditEntry.customer_id == customer.id)
            .order_by(CreditEntry.entry_date.desc(), CreditEntry.id.desc())
            .limit(10)
        )
    ).all()
    return GetCustomerAccountOutput(
        **_customer_summary(customer).model_dump(),
        joined_on=customer.joined_on,
        notes=customer.notes,
        balance=facts.balance,
        available_credit=money(max(Decimal("0"), limit - facts.balance)),
        over_limit_by=money(max(Decimal("0"), facts.balance - limit)),
        open_bills=[
            OpenBillRow(
                sale_id=b.sale_id,
                bought_on=b.entry_date,
                due_on=b.due_on,
                amount_remaining=b.amount_remaining,
                days_past_due=b.days_past_due,
            )
            for b in facts.open_bills
        ],
        max_days_past_due=facts.max_days_past_due,
        last_payment=facts.last_payment,
        reminders_sent_total=len(facts.reminders_sent),
        last_reminder_at=facts.last_reminder_at,
        open_dispute_case_ids=facts.open_dispute_case_ids,
        recent_entries=[
            LedgerRow(
                entry_date=e.entry_date,
                entry_type=str(e.entry_type),
                amount=money(e.amount),
                note=e.note,
            )
            for e in recent
        ],
    )


# ---------------------------------------------------------- purchase orders


POStatus = Literal["draft", "placed", "partially_received", "received", "cancelled"]


class GetPurchaseOrdersInput(ToolInput):
    statuses: list[POStatus] = Field(
        default=["draft", "placed", "partially_received"], description="Default: open orders"
    )
    supplier_id: str | None = None
    product_id: str | None = None
    limit: int = Field(default=20, ge=1, le=50)


class POLine(ToolOutput):
    product_id: str
    quantity_ordered: int
    quantity_received: int
    unit_cost: Decimal


class PurchaseOrderRow(ToolOutput):
    purchase_order_id: str
    supplier_id: str
    supplier_name: str
    status: str
    ordered_at: datetime | None
    expected_on: date | None
    received_at: datetime | None
    days_late: int
    total_amount: Decimal
    notes: str | None
    created_by: str
    items: list[POLine]


class GetPurchaseOrdersOutput(ToolOutput):
    orders: list[PurchaseOrderRow]


async def get_purchase_orders(
    ctx: ToolContext, args: GetPurchaseOrdersInput
) -> GetPurchaseOrdersOutput:
    query = (
        select(PurchaseOrder, Supplier.name)
        .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
        .where(PurchaseOrder.shop_id == ctx.shop_id, PurchaseOrder.status.in_(args.statuses))
    )
    if args.supplier_id:
        query = query.where(PurchaseOrder.supplier_id == args.supplier_id)
    rows = (await ctx.session.execute(query.order_by(PurchaseOrder.id.desc()))).all()
    orders = []
    for po, supplier_name in rows:
        if args.product_id and all(i.product_id != args.product_id for i in po.items):
            continue
        orders.append(
            PurchaseOrderRow(
                purchase_order_id=po.id,
                supplier_id=po.supplier_id,
                supplier_name=supplier_name,
                status=str(po.status),
                ordered_at=to_ist(po.ordered_at),
                expected_on=po.expected_on,
                received_at=to_ist(po.received_at),
                days_late=days_late(po, ctx.today),
                total_amount=money(po.total_amount),
                notes=po.notes,
                created_by=po.created_by,
                items=[
                    POLine(
                        product_id=i.product_id,
                        quantity_ordered=i.quantity_ordered,
                        quantity_received=i.quantity_received,
                        unit_cost=money(i.unit_cost),
                    )
                    for i in po.items
                ],
            )
        )
        if len(orders) >= args.limit:
            break
    return GetPurchaseOrdersOutput(orders=orders)


class GetSupplierInput(ToolInput):
    supplier_id: str = ID


class GetSupplierOutput(ToolOutput):
    supplier_id: str
    name: str
    contact_person: str
    categories: list[str]
    payment_terms_days: int
    lead_time_days: int
    min_order_value: Decimal
    is_active: bool
    orders_from_this_shop: int
    late_or_short_orders: int = Field(description="Orders that are late now or arrived short")


async def get_supplier(ctx: ToolContext, args: GetSupplierInput) -> GetSupplierOutput:
    supplier = await get_supplier_row(ctx, args.supplier_id)
    orders = (
        await ctx.session.scalars(
            select(PurchaseOrder).where(
                PurchaseOrder.shop_id == ctx.shop_id, PurchaseOrder.supplier_id == supplier.id
            )
        )
    ).all()
    troubled = sum(
        1 for po in orders if str(po.status) == "partially_received" or days_late(po, ctx.today) > 0
    )
    return GetSupplierOutput(
        supplier_id=supplier.id,
        name=supplier.name,
        contact_person=supplier.contact_person,
        categories=supplier.categories.split(","),
        payment_terms_days=supplier.payment_terms_days,
        lead_time_days=supplier.lead_time_days,
        min_order_value=money(supplier.min_order_value),
        is_active=supplier.is_active,
        orders_from_this_shop=len(orders),
        late_or_short_orders=troubled,
    )


# --------------------------------------------------------------------- cases


class CaseHistoryInput(ToolInput):
    customer_id: str | None = None
    product_id: str | None = None
    supplier_id: str | None = None
    category: str | None = None
    status: Literal["open", "resolved"] | None = None
    limit: int = Field(default=10, ge=1, le=50)


class CaseRow(ToolOutput):
    case_id: str
    category: str
    title: str
    description: str
    status: str
    resolution: str | None
    opened_at: datetime
    resolved_at: datetime | None


class CaseHistoryOutput(ToolOutput):
    cases: list[CaseRow] = Field(description="Newest first")


async def get_case_history(ctx: ToolContext, args: CaseHistoryInput) -> CaseHistoryOutput:
    conditions = [Case.shop_id == ctx.shop_id]
    for column, value in (
        (Case.customer_id, args.customer_id),
        (Case.product_id, args.product_id),
        (Case.supplier_id, args.supplier_id),
        (Case.category, args.category),
        (Case.status, args.status),
    ):
        if value:
            conditions.append(column == value)
    rows = await ctx.session.scalars(
        select(Case).where(*conditions).order_by(Case.opened_at.desc(), Case.id).limit(args.limit)
    )
    return CaseHistoryOutput(
        cases=[
            CaseRow(
                case_id=c.id,
                category=str(c.category),
                title=c.title,
                description=c.description,
                status=str(c.status),
                resolution=c.resolution,
                opened_at=to_ist(c.opened_at),
                resolved_at=to_ist(c.resolved_at),
            )  # type: ignore[arg-type]
            for c in rows
        ]
    )


def _spec(name: str, description: str, handler, inp, out, **kw) -> ToolSpec:  # type: ignore[no-untyped-def]
    return ToolSpec(
        name=name,
        description=description,
        input_model=inp,
        output_model=out,
        handler=handler,
        kind="read",
        timeout_seconds=kw.get("timeout", 5.0),
        max_retries=kw.get("retries", 1),
    )


DATABASE_TOOLS = [
    _spec(
        "search_products",
        "Find products in this shop by part of the name or by category.",
        search_products,
        SearchProductsInput,
        SearchProductsOutput,
    ),
    _spec(
        "get_product",
        "Full details for one product: stock, reorder level, cost, price, "
        "margin, preferred supplier, open orders and recent sales.",
        get_product_details,
        GetProductInput,
        GetProductOutput,
    ),
    _spec(
        "get_low_stock_products",
        "Active products at or below their reorder level, with any open purchase orders for them.",
        get_low_stock_products,
        LowStockInput,
        LowStockOutput,
    ),
    _spec(
        "get_slow_moving_products",
        "Products with stock on hand but no sales in the last N days (dead stock).",
        get_slow_moving_products,
        SlowMovingInput,
        SlowMovingOutput,
    ),
    _spec(
        "get_stock_movements",
        "Recent stock changes for one product: sales, deliveries, "
        "returns, damage and count adjustments.",
        get_stock_movements,
        StockMovementsInput,
        StockMovementsOutput,
    ),
    _spec(
        "get_sales_summary",
        "Sales totals for a date range: bills, amount, discounts, GST, "
        "payment modes and top products.",
        get_sales_summary,
        SalesSummaryInput,
        SalesSummaryOutput,
    ),
    _spec(
        "get_sale",
        "One bill with its items, customer and status.",
        get_sale,
        GetSaleInput,
        GetSaleOutput,
    ),
    _spec(
        "search_customers",
        "Find customers of this shop by part of the name or phone number.",
        search_customers,
        SearchCustomersInput,
        SearchCustomersOutput,
    ),
    _spec(
        "get_customer_account",
        "A customer's credit account: limit, balance, unpaid bills "
        "with days past due, last payment, reminders sent and open disputes.",
        get_customer_account,
        GetCustomerAccountInput,
        GetCustomerAccountOutput,
    ),
    _spec(
        "get_purchase_orders",
        "Purchase orders (open ones by default) with items, expected date and how many days late.",
        get_purchase_orders,
        GetPurchaseOrdersInput,
        GetPurchaseOrdersOutput,
    ),
    _spec(
        "get_supplier",
        "A supplier's terms (lead time, payment terms, minimum order) and its "
        "record with this shop.",
        get_supplier,
        GetSupplierInput,
        GetSupplierOutput,
    ),
    _spec(
        "get_case_history",
        "Past and open cases for a customer, product, supplier or category, "
        "with how they were resolved.",
        get_case_history,
        CaseHistoryInput,
        CaseHistoryOutput,
    ),
]
