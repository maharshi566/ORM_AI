from collections import Counter
from decimal import Decimal

import pytest

from app.seed.generator import Dataset, generate, validate


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return generate()


def test_same_seed_gives_identical_data(dataset: Dataset) -> None:
    assert generate().fingerprint() == dataset.fingerprint()


def test_different_seed_gives_different_data(dataset: Dataset) -> None:
    assert generate(seed=7).fingerprint() != dataset.fingerprint()


def test_data_is_internally_consistent(dataset: Dataset) -> None:
    assert validate(dataset) == []


def test_volumes_match_the_spec(dataset: Dataset) -> None:
    counts = dataset.counts()
    assert counts["shops"] == 50
    assert counts["users"] == 101  # an owner and a staff member per shop, plus the admin
    assert counts["suppliers"] == 12
    assert 900 <= counts["customers"] <= 1200
    assert 4000 <= counts["sales"] <= 7000
    assert counts["cases"] == 150  # about three past cases per shop
    assert counts["purchase_orders"] >= 400


def test_every_shop_has_a_working_history(dataset: Dataset) -> None:
    for table in ("customers", "products", "sales", "cases"):
        shops_with_rows = {row["shop_id"] for row in dataset.rows(table)}
        assert shops_with_rows == {s["id"] for s in dataset.shops}, table
    cases_per_shop = Counter(case["shop_id"] for case in dataset.cases)
    assert min(cases_per_shop.values()) >= 2


def test_planted_edge_cases_stay_in_the_five_hand_tuned_shops(dataset: Dataset) -> None:
    hand_tuned = {f"SHOP-00{n}" for n in range(1, 6)}
    assert {case["shop_id"] for case in dataset.edge_cases.values()} <= hand_tuned
    assert len(dataset.edge_cases) == 17


def test_every_reference_points_at_a_real_row(dataset: Dataset) -> None:
    ids = {
        table: {row["id"] for row in dataset.rows(table)}
        for table in ("shops", "suppliers", "customers", "products", "sales", "purchase_orders")
    }
    for sale in dataset.sales:
        assert sale["shop_id"] in ids["shops"]
        assert sale["customer_id"] is None or sale["customer_id"] in ids["customers"]
    for item in dataset.sale_items:
        assert item["sale_id"] in ids["sales"] and item["product_id"] in ids["products"]
    for item in dataset.purchase_order_items:
        assert item["purchase_order_id"] in ids["purchase_orders"]
    for entry in dataset.credit_ledger:
        assert entry["customer_id"] in ids["customers"]


def test_customers_only_see_their_own_shop(dataset: Dataset) -> None:
    customer_shop = {c["id"]: c["shop_id"] for c in dataset.customers}
    for sale in dataset.sales:
        if sale["customer_id"]:
            assert customer_shop[sale["customer_id"]] == sale["shop_id"]


def test_credit_only_for_customers_allowed_credit(dataset: Dataset) -> None:
    allowed = {c["id"] for c in dataset.customers if c["credit_allowed"]}
    for sale in dataset.sales:
        if sale["payment_mode"] == "credit":
            assert sale["customer_id"] in allowed


def test_planted_edge_cases_hold(dataset: Dataset) -> None:
    cases = dataset.edge_cases
    assert len(cases) >= 15

    over = cases["over_credit_limit"]
    assert Decimal(over["balance"]) > Decimal(over["credit_limit"])

    products = {p["id"]: p for p in dataset.products}
    low = products[cases["low_stock_no_po"]["product_id"]]
    assert low["stock_qty"] <= low["reorder_level"]

    ghee = products[cases["negative_margin"]["product_id"]]
    assert ghee["cost_price"] > ghee["selling_price"]

    draft = next(
        po
        for po in dataset.purchase_orders
        if po["id"] == cases["high_value_draft_po"]["purchase_order_id"]
    )
    assert draft["status"] == "draft" and draft["total_amount"] > Decimal("10000")

    sold = Counter(item["product_id"] for item in dataset.sale_items)
    assert sold[cases["dead_stock"]["product_id"]] == 0


def test_duplicate_payment_leaves_a_negative_balance(dataset: Dataset) -> None:
    customer_id = dataset.edge_cases["duplicate_payment"]["customer_id"]
    balance = sum(e["amount"] for e in dataset.credit_ledger if e["customer_id"] == customer_id)
    assert balance < 0
