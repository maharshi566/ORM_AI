"""Action tools: approval thresholds, shop rules, idempotency and the audit trail."""

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.models import AuditLog, Notification, Product, PurchaseOrder
from app.tools.api_tools import FailureMode, FaultInjector
from app.tools.base import IST
from tests.conftest import ANCHOR_NOON, OWNER, STAFF

AGENT = "action"


async def act(registry, ctx, name, **arguments):
    return await registry.call(name, arguments, ctx, agent=AGENT)


def po_args(product_id: str, quantity: int, supplier: str, key: str, submit: bool = False) -> dict:
    return {
        "supplier_id": supplier,
        "lines": [{"product_id": product_id, "quantity": quantity}],
        "submit_to_supplier": submit,
        "idempotency_key": key,
    }


# ------------------------------------------------------------ purchase orders


async def test_draft_order_needs_no_approval(
    registry, make_ctx, seed_data, session_factory
) -> None:
    toor = seed_data.edge_cases["low_stock_no_po"]["product_id"]
    result = await act(
        registry,
        make_ctx(),
        "create_purchase_order",
        **po_args(toor, 24, "SUP-002", "t:draft:toor"),
    )

    assert result.ok and result.data["status"] == "draft"
    async with session_factory() as session:
        log = await session.scalar(
            select(AuditLog).where(AuditLog.entity_id == result.data["purchase_order_id"])
        )
    assert log is not None and log.action == "create_purchase_order"


async def test_sending_an_order_needs_approval(
    registry, make_ctx, seed_data, session_factory
) -> None:
    toor = seed_data.edge_cases["low_stock_no_po"]["product_id"]
    args = po_args(toor, 24, "SUP-002", "t:send:toor", submit=True)

    denied = await act(registry, make_ctx(), "create_purchase_order", **args)
    approved = await act(registry, make_ctx(approval=STAFF), "create_purchase_order", **args)

    assert denied.error_code == "approval_required"
    assert denied.error_details["required_role"] == "staff"
    assert approved.ok and approved.data["status"] == "placed"
    assert approved.data["supplier_ref"].startswith("SUP-002-")
    assert approved.data["expected_on"] == str(ANCHOR_NOON.date() + timedelta(days=3))
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(PurchaseOrder)
            .where(PurchaseOrder.idempotency_key == "t:send:toor")
        )
    assert count == 1  # the denied attempt created nothing


async def test_orders_over_10000_need_the_owner(registry, make_ctx, seed_data) -> None:
    paint = next(
        p["id"]
        for p in seed_data.products
        if p["shop_id"] == "SHOP-002" and p["name"] == "Exterior Emulsion Paint 4L"
    )
    args = po_args(paint, 10, "SUP-005", "t:send:paint", submit=True)

    by_staff = await act(
        registry, make_ctx("SHOP-002", approval=STAFF), "create_purchase_order", **args
    )
    by_owner = await act(
        registry, make_ctx("SHOP-002", approval=OWNER), "create_purchase_order", **args
    )

    assert by_staff.error_code == "approval_required"
    assert by_staff.error_details["required_role"] == "owner"
    assert by_owner.ok and Decimal(by_owner.data["total_amount"]) > 10000


async def test_no_second_order_while_one_is_open(registry, make_ctx, seed_data) -> None:
    case = seed_data.edge_cases["low_stock_with_open_po"]
    result = await act(
        registry,
        make_ctx(),
        "create_purchase_order",
        **po_args(case["product_id"], 30, "SUP-002", "t:dup:oil"),
    )

    assert result.error_code == "conflict"
    assert case["purchase_order_id"] in str(result.error_details)


async def test_supplier_minimum_order_value(registry, make_ctx, seed_data) -> None:
    salt = next(
        p["id"]
        for p in seed_data.products
        if p["shop_id"] == "SHOP-001" and p["name"] == "Iodised Salt 1kg"
    )
    result = await act(
        registry,
        make_ctx(approval=OWNER),
        "create_purchase_order",
        **po_args(salt, 2, "SUP-001", "t:min:salt", submit=True),
    )

    assert result.error_code == "policy_blocked"


async def test_inactive_products_cannot_be_ordered(registry, make_ctx, seed_data) -> None:
    pen = seed_data.edge_cases["inactive_product_with_stock"]["product_id"]
    result = await act(
        registry,
        make_ctx("SHOP-003"),
        "create_purchase_order",
        **po_args(pen, 10, "SUP-007", "t:inactive:pen"),
    )

    assert result.error_code == "policy_blocked"


async def test_same_idempotency_key_never_orders_twice(
    registry, make_ctx, seed_data, session_factory
) -> None:
    toor = seed_data.edge_cases["low_stock_no_po"]["product_id"]
    args = po_args(toor, 24, "SUP-002", "t:idem:toor", submit=True)

    first = await act(registry, make_ctx(approval=STAFF), "create_purchase_order", **args)
    second = await act(registry, make_ctx(approval=STAFF), "create_purchase_order", **args)

    assert first.ok and second.ok
    assert second.data["replayed"] is True
    assert second.data["purchase_order_id"] == first.data["purchase_order_id"]
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(PurchaseOrder)
            .where(PurchaseOrder.idempotency_key == "t:idem:toor")
        )
    assert count == 1


# ---------------------------------------------------------- stock adjustment


async def test_stock_adjustment_thresholds(registry, make_ctx, seed_data, session_factory) -> None:
    cement = seed_data.edge_cases["late_purchase_order"]["product_id"]  # Rs 360 a bag at cost
    small = {
        "product_id": cement,
        "quantity_change": -2,
        "reason": "Two torn bags found today",
        "movement_type": "damage",
        "idempotency_key": "t:adj:small",
    }
    large = {**small, "quantity_change": -3, "idempotency_key": "t:adj:large"}

    ok_small = await act(
        registry, make_ctx("SHOP-002", approval=STAFF), "record_stock_adjustment", **small
    )
    staff_large = await act(
        registry, make_ctx("SHOP-002", approval=STAFF), "record_stock_adjustment", **large
    )
    replay = await act(
        registry, make_ctx("SHOP-002", approval=STAFF), "record_stock_adjustment", **small
    )

    assert ok_small.ok and ok_small.data["new_stock_qty"] == 4
    assert staff_large.error_code == "approval_required"  # Rs 1,080 > Rs 1,000
    assert replay.data["replayed"] is True
    async with session_factory() as session:
        stock = await session.scalar(select(Product.stock_qty).where(Product.id == cement))
    assert stock == 4  # the replay did not remove stock again


async def test_stock_cannot_go_below_zero(registry, make_ctx, seed_data) -> None:
    toor = seed_data.edge_cases["low_stock_no_po"]["product_id"]  # 2 in stock
    result = await act(
        registry,
        make_ctx(approval=OWNER),
        "record_stock_adjustment",
        product_id=toor,
        quantity_change=-5,
        reason="Count found nothing left",
        idempotency_key="t:adj:negative",
    )

    assert result.error_code == "invalid_input"


# ---------------------------------------------------------- payment reminders


async def _remindable_customer(registry, make_ctx, seed_data) -> tuple[str, str, datetime]:
    """Find an overdue, undisputed customer and a time when a reminder is allowed."""
    planted = {
        seed_data.edge_cases[k]["customer_id"]
        for k in ("overdue_credit", "reminder_sent_recently", "duplicate_payment")
    }
    for customer in seed_data.customers:
        if not customer["credit_allowed"] or customer["id"] in planted:
            continue
        result = await registry.call(
            "get_customer_account",
            {"customer_id": customer["id"]},
            make_ctx(customer["shop_id"]),
            agent="data_retrieval",
        )
        data = result.data
        if (
            data["max_days_past_due"] >= 7
            and not data["open_dispute_case_ids"]
            and data["reminders_sent_total"] < 3
        ):
            last = (
                datetime.fromisoformat(data["last_reminder_at"])
                if data["last_reminder_at"]
                else ANCHOR_NOON
            )
            when = max(ANCHOR_NOON, last + timedelta(days=8)).replace(hour=12, minute=0)
            return customer["shop_id"], customer["id"], when
    raise AssertionError("no remindable customer in the synthetic data")


async def test_reminder_is_sent_when_the_policy_allows(
    registry, make_ctx, seed_data, session_factory
) -> None:
    shop_id, customer_id, when = await _remindable_customer(registry, make_ctx, seed_data)
    ctx = make_ctx(shop_id, now=when)

    result = await act(
        registry,
        ctx,
        "send_payment_reminder",
        customer_id=customer_id,
        idempotency_key="t:remind:ok",
    )

    assert result.ok, result
    assert "gentle reminder" in result.data["message"]
    assert ctx.clients["messaging_api"].sent  # the gateway was called
    async with session_factory() as session:
        saved = await session.scalar(
            select(Notification).where(Notification.idempotency_key == "t:remind:ok")
        )
    assert saved is not None and str(saved.status) == "sent"


async def test_reminder_rules_block_what_the_policy_forbids(registry, make_ctx, seed_data) -> None:
    e = seed_data.edge_cases
    recent = await act(
        registry,
        make_ctx(),
        "send_payment_reminder",
        customer_id=e["reminder_sent_recently"]["customer_id"],
        idempotency_key="t:remind:recent",
    )
    disputed = await act(
        registry,
        make_ctx(),
        "send_payment_reminder",
        customer_id=e["overdue_credit"]["customer_id"],
        idempotency_key="t:remind:dispute",
    )
    not_due = await act(
        registry,
        make_ctx(),
        "send_payment_reminder",
        customer_id=e["over_credit_limit"]["customer_id"],
        idempotency_key="t:remind:notdue",
    )

    assert recent.error_code == "policy_blocked" and "last_reminder_at" in recent.error_details
    assert disputed.error_code == "policy_blocked" and "case_ids" in disputed.error_details
    assert not_due.error_code == "policy_blocked"


async def test_no_reminders_outside_9am_to_8pm(registry, make_ctx, seed_data) -> None:
    shop_id, customer_id, when = await _remindable_customer(registry, make_ctx, seed_data)
    late = when.replace(hour=21)

    result = await act(
        registry,
        make_ctx(shop_id, now=late),
        "send_payment_reminder",
        customer_id=customer_id,
        idempotency_key="t:remind:late",
    )

    assert result.error_code == "policy_blocked"


async def test_gateway_failure_sends_nothing(
    registry, make_ctx, seed_data, session_factory
) -> None:
    shop_id, customer_id, when = await _remindable_customer(registry, make_ctx, seed_data)
    ctx = make_ctx(shop_id, now=when, faults=FaultInjector(FailureMode.SERVER_ERROR))

    result = await act(
        registry,
        ctx,
        "send_payment_reminder",
        customer_id=customer_id,
        idempotency_key="t:remind:gateway",
    )

    assert result.error_code == "upstream_error" and result.retryable
    async with session_factory() as session:
        saved = await session.scalar(
            select(Notification).where(Notification.idempotency_key == "t:remind:gateway")
        )
    assert saved is None


# ------------------------------------------------------------- price changes


async def test_price_rules_then_owner_approval(registry, make_ctx, seed_data) -> None:
    ghee = seed_data.edge_cases["negative_margin"]["product_id"]  # cost 352, MRP 360
    base = {"product_id": ghee, "reason": "Supplier raised the cost to Rs 352"}

    above_mrp = await act(
        registry,
        make_ctx("SHOP-004", approval=OWNER),
        "update_selling_price",
        **base,
        new_selling_price="365",
        idempotency_key="t:price:mrp",
    )
    below_cost = await act(
        registry,
        make_ctx("SHOP-004", approval=OWNER),
        "update_selling_price",
        **base,
        new_selling_price="345",
        idempotency_key="t:price:cost",
    )
    by_staff = await act(
        registry,
        make_ctx("SHOP-004", approval=STAFF),
        "update_selling_price",
        **base,
        new_selling_price="360",
        idempotency_key="t:price:staff",
    )
    by_owner = await act(
        registry,
        make_ctx("SHOP-004", approval=OWNER),
        "update_selling_price",
        **base,
        new_selling_price="360",
        idempotency_key="t:price:owner",
    )

    assert above_mrp.error_code == "policy_blocked"
    assert below_cost.error_code == "policy_blocked"
    assert by_staff.error_code == "approval_required"
    assert by_owner.ok and by_owner.data["new_price"] == "360.00"
    assert by_owner.data["warnings"]  # margin below the 8% minimum is flagged


# ------------------------------------------------------------------- returns


def _recent_sale(
    seed_data, shop_id: str, *, max_total: int | None = None, min_total: int | None = None
) -> str:
    for sale in reversed(seed_data.sales):
        if (
            sale["shop_id"] == shop_id
            and sale["status"] == "completed"
            and sale["payment_mode"] != "credit"
            and (ANCHOR_NOON.date() - sale["sold_at"].date()).days <= 5
            and (max_total is None or sale["total"] <= max_total)
            and (min_total is None or sale["total"] > min_total)
        ):
            return sale["id"]
    raise AssertionError("no suitable sale")


async def test_returns(registry, make_ctx, seed_data) -> None:
    small = _recent_sale(seed_data, "SHOP-005", max_total=2000)
    big = _recent_sale(seed_data, "SHOP-002", min_total=2000)
    old = next(
        s["id"]
        for s in seed_data.sales
        if s["shop_id"] == "SHOP-001" and s["status"] == "completed"
    )
    returned = seed_data.edge_cases["returned_sale"]["sale_id"]

    ok = await act(
        registry,
        make_ctx("SHOP-005", approval=STAFF),
        "process_return",
        sale_id=small,
        reason="Wrong item",
        idempotency_key="t:ret:small",
    )
    big_by_staff = await act(
        registry,
        make_ctx("SHOP-002", approval=STAFF),
        "process_return",
        sale_id=big,
        reason="Not needed",
        idempotency_key="t:ret:big",
    )
    too_old = await act(
        registry,
        make_ctx(approval=OWNER),
        "process_return",
        sale_id=old,
        reason="Too late",
        idempotency_key="t:ret:old",
    )
    again = await act(
        registry,
        make_ctx("SHOP-005", approval=OWNER),
        "process_return",
        sale_id=returned,
        reason="Second try",
        idempotency_key="t:ret:again",
    )

    assert ok.ok and ok.data["refund_method"] in {"cash", "upi", "card"}
    assert big_by_staff.error_code == "approval_required"
    assert too_old.error_code == "policy_blocked"
    assert again.error_code == "conflict"


# ---------------------------------------------------------- supplier follow-up


def follow_up(po_id: str, issue: str, key: str) -> dict:
    return {"purchase_order_id": po_id, "issue": issue, "idempotency_key": key}


async def test_late_order_follow_up_needs_staff_and_escalates(
    registry, make_ctx, seed_data, session_factory
) -> None:
    late = seed_data.edge_cases["late_purchase_order"]
    args = follow_up(late["purchase_order_id"], "late", "t:follow:late")

    denied = await act(registry, make_ctx("SHOP-002"), "follow_up_supplier", **args)
    sent = await act(registry, make_ctx("SHOP-002", approval=STAFF), "follow_up_supplier", **args)
    again = await act(registry, make_ctx("SHOP-002", approval=STAFF), "follow_up_supplier", **args)
    tomorrow_same_day = await act(
        registry,
        make_ctx("SHOP-002", approval=STAFF),
        "follow_up_supplier",
        **follow_up(late["purchase_order_id"], "late", "t:follow:late:2"),
    )

    assert denied.error_code == "approval_required"
    assert sent.ok, sent.error_message
    assert sent.data["days_late"] == late["days_late"]
    assert sent.data["escalate_to_owner"] is True  # 3+ days late: tell the owner
    assert "owner decides" in sent.data["next_step"]  # 7 days late
    assert late["purchase_order_id"] in sent.data["message"]
    assert "x" in sent.data["sent_to"]  # the supplier's number is masked
    assert again.data["replayed"] is True
    assert tomorrow_same_day.error_code == "policy_blocked"  # once per order per day
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(Notification).where(Notification.purpose == "supplier_follow_up")
            )
        ).all()
        log = await session.scalar(select(AuditLog).where(AuditLog.action == "follow_up_supplier"))
    assert len(rows) == 1 and rows[0].supplier_id == sent.data["supplier_id"]
    assert log.entity_id == late["purchase_order_id"]


async def test_short_delivery_follow_up_lists_the_missing_items(
    registry, make_ctx, seed_data
) -> None:
    short = seed_data.edge_cases["short_delivery"]

    sent = await act(
        registry,
        make_ctx("SHOP-003", approval=STAFF),
        "follow_up_supplier",
        **follow_up(short["purchase_order_id"], "short", "t:follow:short"),
    )

    assert sent.ok, sent.error_message
    [item] = sent.data["missing_items"]
    assert item["product_id"] == short["product_id"]
    assert item["missing"] == short["ordered"] - short["received"]
    assert "credit note" in sent.data["message"]
    assert "SOP-SHORT-001" in sent.data["next_step"]


async def test_follow_up_refuses_orders_that_are_fine(registry, make_ctx, seed_data) -> None:
    on_time = seed_data.edge_cases["low_stock_with_open_po"]["purchase_order_id"]  # due in 2 days
    late = seed_data.edge_cases["late_purchase_order"]["purchase_order_id"]

    not_late = await act(
        registry,
        make_ctx(approval=STAFF),
        "follow_up_supplier",
        **follow_up(on_time, "late", "t:follow:ontime"),
    )
    not_short = await act(
        registry,
        make_ctx("SHOP-002", approval=STAFF),
        "follow_up_supplier",
        **follow_up(late, "short", "t:follow:notshort"),
    )
    other_shop = await act(
        registry,
        make_ctx("SHOP-001", approval=STAFF),
        "follow_up_supplier",
        **follow_up(late, "late", "t:follow:othershop"),
    )

    assert not_late.error_code == "policy_blocked"
    assert not_short.error_code == "policy_blocked"
    assert other_shop.error_code == "not_found"  # SHOP-002's order is invisible to SHOP-001


async def test_follow_up_messaging_failure_saves_nothing(
    registry, make_ctx, seed_data, session_factory
) -> None:
    late = seed_data.edge_cases["late_purchase_order"]["purchase_order_id"]

    failed = await act(
        registry,
        make_ctx("SHOP-002", approval=STAFF, faults=FaultInjector(FailureMode.SERVER_ERROR)),
        "follow_up_supplier",
        **follow_up(late, "late", "t:follow:fail"),
    )

    assert failed.error_code == "upstream_error"
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.purpose == "supplier_follow_up")
        )
    assert count == 0


# --------------------------------------------------------------------- cases


async def test_cases_open_freely_but_closing_needs_staff(
    registry, make_ctx, session_factory
) -> None:
    args = {
        "category": "reorder",
        "title": "Sugar running out before Diwali",
        "description": "Sugar is at 1 packet with no order placed.",
    }

    opened = await act(registry, make_ctx(), "create_case", **args)
    duplicate = await act(registry, make_ctx(), "create_case", **args)
    close_args = {
        "case_id": opened.data["case_id"],
        "resolution": "Ordered 40 packets from SUP-001.",
    }
    denied = await act(registry, make_ctx(), "resolve_case", **close_args)
    closed = await act(registry, make_ctx(approval=STAFF), "resolve_case", **close_args)

    assert opened.ok and opened.data["case_id"].startswith("CASE-")
    assert duplicate.data["replayed"] is True
    assert denied.error_code == "approval_required"
    assert closed.ok and closed.data["status"] == "resolved"
    async with session_factory() as session:
        log = await session.scalar(select(AuditLog).where(AuditLog.action == "resolve_case"))
    assert log.details["approved_by"] == STAFF.approved_by


def test_ist_constant_is_india() -> None:
    assert datetime(2026, 1, 1, tzinfo=IST).utcoffset() == timedelta(hours=5, minutes=30)
