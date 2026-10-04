"""Read tools return the right facts, and only for the caller's own shop."""

from collections import defaultdict
from decimal import Decimal

import pytest

AGENT = "data_retrieval"


async def call(registry, ctx, name, **arguments):
    result = await registry.call(name, arguments, ctx, agent=AGENT)
    assert result.ok, result
    return result.data


async def test_low_stock_matches_the_records(registry, make_ctx, seed_data) -> None:
    expected = {
        p["id"]
        for p in seed_data.products
        if p["shop_id"] == "SHOP-001" and p["is_active"] and p["stock_qty"] <= p["reorder_level"]
    }
    data = await call(registry, make_ctx(), "get_low_stock_products")

    by_id = {item["product_id"]: item for item in data["items"]}
    assert set(by_id) == expected
    no_po = seed_data.edge_cases["low_stock_no_po"]["product_id"]
    on_order = seed_data.edge_cases["low_stock_with_open_po"]
    assert by_id[no_po]["open_orders"] == []
    assert (
        by_id[on_order["product_id"]]["open_orders"][0]["purchase_order_id"]
        == on_order["purchase_order_id"]
    )
    assert data["with_open_order"] + data["without_open_order"] == len(expected)


async def test_shop_scope_hides_other_shops_records(registry, make_ctx, seed_data) -> None:
    other_product = seed_data.edge_cases["negative_margin"]["product_id"]  # SHOP-004
    other_customer = seed_data.edge_cases["duplicate_payment"]["customer_id"]  # SHOP-002

    product = await registry.call(
        "get_product", {"product_id": other_product}, make_ctx(), agent=AGENT
    )
    account = await registry.call(
        "get_customer_account", {"customer_id": other_customer}, make_ctx(), agent=AGENT
    )

    assert product.error_code == "not_found"
    assert account.error_code == "not_found"


async def test_negative_margin_is_visible(registry, make_ctx, seed_data) -> None:
    case = seed_data.edge_cases["negative_margin"]
    data = await call(registry, make_ctx("SHOP-004"), "get_product", product_id=case["product_id"])

    assert Decimal(data["cost_price"]) == Decimal(case["cost_price"])
    assert data["margin_percent"] < 0


async def test_customer_account_balance_and_limit(registry, make_ctx, seed_data) -> None:
    case = seed_data.edge_cases["over_credit_limit"]
    ledger = sum(
        e["amount"] for e in seed_data.credit_ledger if e["customer_id"] == case["customer_id"]
    )

    data = await call(registry, make_ctx(), "get_customer_account", customer_id=case["customer_id"])

    assert Decimal(data["balance"]) == ledger
    assert Decimal(data["over_limit_by"]) == ledger - Decimal(case["credit_limit"])
    assert Decimal(data["available_credit"]) == 0
    assert "x" in data["phone"]  # masked


async def test_overdue_account_shows_age_reminders_and_dispute(
    registry, make_ctx, seed_data
) -> None:
    case = seed_data.edge_cases["overdue_credit"]
    data = await call(registry, make_ctx(), "get_customer_account", customer_id=case["customer_id"])

    assert data["max_days_past_due"] == case["days_overdue"]
    assert data["reminders_sent_total"] == 3
    assert data["open_dispute_case_ids"]


async def test_duplicate_payment_shows_a_negative_balance(registry, make_ctx, seed_data) -> None:
    case = seed_data.edge_cases["duplicate_payment"]
    data = await call(
        registry, make_ctx("SHOP-002"), "get_customer_account", customer_id=case["customer_id"]
    )

    assert Decimal(data["balance"]) < 0
    notes = [e["note"] for e in data["recent_entries"] if e["entry_type"] == "payment"]
    assert notes.count(f"Paid by UPI, ref {case['payment_ref']}") == 2


async def test_late_purchase_order(registry, make_ctx, seed_data) -> None:
    case = seed_data.edge_cases["late_purchase_order"]
    data = await call(
        registry, make_ctx("SHOP-002"), "get_purchase_orders", product_id=case["product_id"]
    )

    order = next(o for o in data["orders"] if o["purchase_order_id"] == case["purchase_order_id"])
    assert order["days_late"] == case["days_late"]
    assert order["status"] == "placed"


async def test_sales_summary_matches_the_bills(registry, make_ctx, seed_data) -> None:
    bills = [
        s for s in seed_data.sales if s["shop_id"] == "SHOP-003" and s["status"] == "completed"
    ]
    by_mode: dict[str, Decimal] = defaultdict(Decimal)
    for bill in bills:
        by_mode[bill["payment_mode"]] += bill["total"]

    data = await call(
        registry,
        make_ctx("SHOP-003"),
        "get_sales_summary",
        start_date="2026-06-01",
        end_date="2026-09-30",
    )

    assert data["bills"] == len(bills)
    assert Decimal(data["total_sales"]) == sum(b["total"] for b in bills)
    assert {k: Decimal(v) for k, v in data["by_payment_mode"].items()} == dict(by_mode)
    assert len(data["top_products"]) == 5


@pytest.mark.parametrize("start,end", [("2026-09-30", "2026-09-01"), ("2025-01-01", "2026-09-30")])
async def test_sales_summary_rejects_bad_ranges(registry, make_ctx, start, end) -> None:
    result = await registry.call(
        "get_sales_summary", {"start_date": start, "end_date": end}, make_ctx(), agent=AGENT
    )
    assert result.error_code == "invalid_input"


async def test_dead_and_inactive_stock(registry, make_ctx, seed_data) -> None:
    dead = seed_data.edge_cases["dead_stock"]["product_id"]
    data = await call(registry, make_ctx("SHOP-005"), "get_slow_moving_products", days=90)

    assert dead in [item["product_id"] for item in data["items"]]


async def test_returned_sale_and_its_age(registry, make_ctx, seed_data) -> None:
    case = seed_data.edge_cases["returned_sale"]
    data = await call(registry, make_ctx("SHOP-005"), "get_sale", sale_id=case["sale_id"])

    assert data["status"] == "returned"
    assert data["days_since_sale"] == 6


async def test_stock_movements_show_the_count_adjustment(registry, make_ctx, seed_data) -> None:
    case = seed_data.edge_cases["stock_count_mismatch"]
    data = await call(registry, make_ctx(), "get_stock_movements", product_id=case["product_id"])

    assert data["totals_by_type"]["adjustment"] == -case["missing_units"]
    assert data["movements"][0]["moved_at"] >= data["movements"][-1]["moved_at"]


async def test_search_customers_by_phone_digits_finds_duplicates(
    registry, make_ctx, seed_data
) -> None:
    case = seed_data.edge_cases["duplicate_customer"]
    data = await call(registry, make_ctx("SHOP-003"), "search_customers", query=case["phone"][-5:])

    assert {c["customer_id"] for c in data["customers"]} == set(case["customer_ids"])


async def test_case_history_and_supplier_record(registry, make_ctx, seed_data) -> None:
    customer = seed_data.edge_cases["overdue_credit"]["customer_id"]
    cases = await call(registry, make_ctx(), "get_case_history", customer_id=customer)
    supplier = await call(registry, make_ctx("SHOP-002"), "get_supplier", supplier_id="SUP-004")

    assert any(c["status"] == "open" for c in cases["cases"])
    assert supplier["lead_time_days"] == 5
    assert supplier["late_or_short_orders"] >= 1


async def test_supplier_price_check(registry, make_ctx, seed_data) -> None:
    case = seed_data.edge_cases["negative_margin"]
    data = await call(
        registry, make_ctx("SHOP-004"), "check_supplier_price", product_id=case["product_id"]
    )

    assert data["supplier_id"] == "SUP-009"
    assert data["margin_percent_at_quote"] < 0
