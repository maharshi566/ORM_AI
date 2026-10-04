"""Deterministic synthetic data for ORM_AI.

``generate()`` simulates ``days`` days of trading for fifty local shops (see
``catalog.SHOPS``) and returns every table's rows. The same ``seed`` and ``anchor``
always produce the same data, which matters because evaluation cases (Phase 8)
expect exact answers. Changing the list of shops or the generator's code changes the
data, so reseed after changing either.

How the simulation works, day by day:

1. Each shop rings up a few bills. Each bill picks products by popularity and
   takes their stock down (a ``sale`` stock movement).
2. Known customers sometimes buy on credit (udhaar). That adds a row to the
   credit ledger, due 30 days later (credit policy v2).
3. Credit customers pay back according to a habit: prompt, regular or slow.
4. Overdue customers get a WhatsApp reminder, at most one every 7 days.
5. At the end of the day, anything at or below its reorder level goes on a
   purchase order to its preferred supplier. Orders arrive after the supplier's
   lead time; a few arrive late or short.
6. Now and then a supplier raises a price, bakery items expire, or a customer
   returns something.

After the simulation, ``_plant_edge_cases`` adds hand-made situations the agents
must handle (low stock, a late supplier, a customer over their credit limit, a
duplicate payment, ...). They are listed in ``Dataset.edge_cases`` and written
to ``data/seed/EDGE_CASES.md`` by the seed script. They all live in the first five
shops (SHOP-001 to SHOP-005); the other forty-five are ordinary trading shops.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from faker import Faker

from app.seed.catalog import CITY, CUSTOMER_MIX, LOCALITIES, PRODUCTS, SHOPS, SUPPLIERS, ShopSpec

DEFAULT_SEED = 42
DEFAULT_ANCHOR = date(2026, 9, 30)  # "today" in the synthetic world
DEFAULT_DAYS = 90

IST = timezone(timedelta(hours=5, minutes=30), "IST")
CENT = Decimal("0.01")
CREDIT_DAYS = 30  # credit policy v2: pay within 30 days
REMINDER_GAP_DAYS = 7  # reminder policy: at most one reminder every 7 days
OVERDUE_GRACE_DAYS = 7  # remind once a bill is 7+ days past its due date

CONSTRUCTION_CATEGORIES = {"cement", "construction", "fasteners", "plumbing"}
PERISHABLE_CATEGORIES = {"milk", "bread", "bakery", "dairy"}

# Order matters: this is the insert order (parents before children).
TABLES = [
    "shops",
    "suppliers",
    "users",
    "customers",
    "products",
    "purchase_orders",
    "purchase_order_items",
    "sales",
    "sale_items",
    "stock_movements",
    "credit_ledger",
    "price_changes",
    "notifications",
    "cases",
]


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Dataset:
    anchor: date
    seed: int
    shops: list[dict[str, Any]] = field(default_factory=list)
    suppliers: list[dict[str, Any]] = field(default_factory=list)
    users: list[dict[str, Any]] = field(default_factory=list)
    customers: list[dict[str, Any]] = field(default_factory=list)
    products: list[dict[str, Any]] = field(default_factory=list)
    purchase_orders: list[dict[str, Any]] = field(default_factory=list)
    purchase_order_items: list[dict[str, Any]] = field(default_factory=list)
    sales: list[dict[str, Any]] = field(default_factory=list)
    sale_items: list[dict[str, Any]] = field(default_factory=list)
    stock_movements: list[dict[str, Any]] = field(default_factory=list)
    credit_ledger: list[dict[str, Any]] = field(default_factory=list)
    price_changes: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    cases: list[dict[str, Any]] = field(default_factory=list)
    edge_cases: dict[str, dict[str, Any]] = field(default_factory=dict)

    def rows(self, table: str) -> list[dict[str, Any]]:
        return getattr(self, table)

    def counts(self) -> dict[str, int]:
        return {table: len(self.rows(table)) for table in TABLES}

    def fingerprint(self) -> str:
        """SHA-256 of every row. Equal fingerprints mean identical data."""
        payload = {table: self.rows(table) for table in TABLES}
        payload["edge_cases"] = self.edge_cases  # type: ignore[assignment]
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()


class _Simulator:
    def __init__(self, seed: int, anchor: date, days: int) -> None:
        self.rng = random.Random(seed)
        self.fake = Faker("en_IN")
        self.fake.seed_instance(seed)
        self.anchor = anchor
        self.start = anchor - timedelta(days=days)
        self.ds = Dataset(anchor=anchor, seed=seed)

        self.shop_specs = {spec.id: spec for spec in SHOPS}
        self.suppliers = {s.id: s for s in SUPPLIERS}
        self.owner: dict[str, str] = {}  # shop_id -> owner user id
        self.staff: dict[str, str] = {}
        self.products: dict[str, dict[str, Any]] = {}
        self.products_by_shop: dict[str, list[str]] = defaultdict(list)
        self.product_by_name: dict[tuple[str, str], str] = {}
        self.demand: dict[str, float] = {}
        self.customers: dict[str, dict[str, Any]] = {}
        self.customers_by_shop: dict[str, list[str]] = defaultdict(list)
        self.profile: dict[str, str] = {}  # customer_id -> prompt / regular / slow
        self.pay_after: dict[str, int] = {}  # customer_id -> days before paying
        self.no_random_credit: set[str] = set()
        self.open_credit: dict[str, list[list[Any]]] = defaultdict(list)  # FIFO [remaining, due_on]
        self.last_reminder: dict[str, date] = {}
        self.open_po_by_product: dict[str, str] = {}
        self.receipts_due: dict[date, list[str]] = defaultdict(list)
        self.pos: dict[str, dict[str, Any]] = {}
        self.po_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.no_reorder_from: dict[str, date] = {}
        self.no_sales: set[str] = set()
        self.no_price_change: set[str] = set()
        self.recent_sales: dict[str, list[str]] = defaultdict(list)
        self.sales_by_id: dict[str, dict[str, Any]] = {}
        self.items_by_sale: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.sim_events: list[dict[str, Any]] = []  # material for historical cases
        self.counters: defaultdict[str, int] = defaultdict(int)
        self.edge_contractor = ""

    # ------------------------------------------------------------------ helpers
    def next_id(self, prefix: str, width: int) -> str:
        self.counters[prefix] += 1
        return f"{prefix}-{self.counters[prefix]:0{width}d}"

    def at(self, day: date, hour: int, minute: int = 0) -> datetime:
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)

    def phone(self, group: int) -> str:
        self.counters[f"phone{group}"] += 1
        return f"+91-00000-{group}{self.counters[f'phone{group}']:04d}"

    def pid(self, shop_id: str, name: str) -> str:
        return self.product_by_name[(shop_id, name)]

    def move(
        self,
        product_id: str,
        qty: int,
        kind: str,
        when: datetime,
        *,
        ref: tuple | None = None,
        reason: str | None = None,
        by: str = "seed",
    ) -> None:
        product = self.products[product_id]
        product["stock_qty"] += qty
        if product["stock_qty"] < 0:
            raise ValueError(f"Stock for {product_id} went negative")
        self.ds.stock_movements.append(
            {
                "shop_id": product["shop_id"],
                "product_id": product_id,
                "movement_type": kind,
                "quantity": qty,
                "reference_type": ref[0] if ref else None,
                "reference_id": ref[1] if ref else None,
                "reason": reason,
                "moved_at": when,
                "created_by": by,
                "idempotency_key": None,
            }
        )

    def balance(self, customer_id: str) -> Decimal:
        return sum((row[0] for row in self.open_credit[customer_id]), Decimal("0"))

    # ------------------------------------------------------------- reference
    def build_reference_data(self) -> None:
        user_no = 0
        for spec in SHOPS:
            self.ds.shops.append(
                {
                    "id": spec.id,
                    "name": spec.name,
                    "shop_type": spec.shop_type,
                    "owner_name": spec.owner_name,
                    "phone": self.phone(1),
                    "locality": spec.locality,
                    "city": CITY,
                    "opened_on": spec.opened_on,
                    "created_at": self.at(self.start, 8),
                }
            )
            for role, name in (("owner", spec.owner_name), ("staff", spec.staff_name)):
                user_no += 1
                user_id = f"USR-{user_no:03d}"
                (self.owner if role == "owner" else self.staff)[spec.id] = user_id
                self.ds.users.append(
                    {
                        "id": user_id,
                        "shop_id": spec.id,
                        "name": name,
                        "role": role,
                        "phone": self.phone(4),
                        "is_active": True,
                        "created_at": self.at(self.start, 8),
                    }
                )
        self.ds.users.append(
            {
                "id": f"USR-{user_no + 1:03d}",
                "shop_id": None,
                "name": "ORM_AI Admin",
                "role": "admin",
                "phone": None,
                "is_active": True,
                "created_at": self.at(self.start, 8),
            }
        )

        for s in SUPPLIERS:
            self.ds.suppliers.append(
                {
                    "id": s.id,
                    "name": s.name,
                    "contact_person": s.contact_person,
                    "phone": self.phone(2),
                    "city": CITY,
                    "categories": s.categories,
                    "payment_terms_days": s.payment_terms_days,
                    "lead_time_days": s.lead_time_days,
                    "min_order_value": money(s.min_order_value),
                    "is_active": True,
                    "created_at": self.at(self.start, 8),
                }
            )

        for spec in SHOPS:
            for index, p in enumerate(PRODUCTS[spec.shop_type], start=1):
                product_id = self.next_id("PRD", 4)
                mrp = math.ceil(p.price * 1.05 / 5) * 5
                row = {
                    "id": product_id,
                    "shop_id": spec.id,
                    "sku": f"{spec.sku_prefix}-{index:03d}",
                    "name": p.name,
                    "category": p.category,
                    "unit": p.unit,
                    "cost_price": money(p.cost),
                    "selling_price": money(p.price),
                    "mrp": money(mrp),
                    "gst_rate": money(p.gst),
                    "stock_qty": 0,
                    "reorder_level": p.reorder_level,
                    "reorder_qty": p.reorder_qty,
                    "preferred_supplier_id": p.supplier_id,
                    "is_active": True,
                    "created_at": self.at(self.start, 8),
                }
                self.ds.products.append(row)
                self.products[product_id] = row
                self.products_by_shop[spec.id].append(product_id)
                self.product_by_name[(spec.id, p.name)] = product_id
                self.demand[product_id] = p.demand

    def build_opening_stock(self) -> None:
        opening_day = self.start - timedelta(days=1)
        for product_id, product in self.products.items():
            qty = product["reorder_level"] + self.rng.randint(
                product["reorder_qty"] // 2, product["reorder_qty"]
            )
            self.move(
                product_id,
                qty,
                "opening",
                self.at(opening_day, 8),
                reason="Stock on hand when records began",
            )

    def build_customers(self) -> None:
        for spec in SHOPS:
            mix = CUSTOMER_MIX[spec.shop_type]
            for _ in range(spec.customers):
                roll = self.rng.random()
                cumulative = 0.0
                ctype, credit_share, limit = mix[-1][0], mix[-1][2], mix[-1][3]
                for kind, share, c_share, c_limit in mix:
                    cumulative += share
                    if roll <= cumulative:
                        ctype, credit_share, limit = kind, c_share, c_limit
                        break
                if ctype == "institution":
                    name = self.rng.choice(
                        [
                            "Little Flower School",
                            "Sunrise Public School",
                            "Bright Minds Tuition Centre",
                        ]
                    )
                elif ctype == "business":
                    name = f"{self.fake.last_name()} Mobile Repairs"
                else:
                    name = self.fake.name()
                credit_allowed = self.rng.random() < credit_share
                joined = self.start - timedelta(days=self.rng.randint(30, 900))
                customer_id = self.next_id("CUST", 4)
                row = {
                    "id": customer_id,
                    "shop_id": spec.id,
                    "name": name,
                    "phone": self.phone(3),
                    "locality": self.rng.choice(LOCALITIES),
                    "customer_type": ctype,
                    "credit_allowed": credit_allowed,
                    "credit_limit": money(limit if credit_allowed else 0),
                    "is_active": True,
                    "notes": None,
                    "joined_on": joined,
                    "created_at": self.at(joined, 10),
                }
                self.ds.customers.append(row)
                self.customers[customer_id] = row
                self.customers_by_shop[spec.id].append(customer_id)
                self.profile[customer_id] = self.rng.choice(
                    ["prompt", "regular", "regular", "slow"]
                )
                self.pay_after[customer_id] = {
                    "prompt": self.rng.randint(5, 14),
                    "regular": self.rng.randint(20, 30),
                    "slow": self.rng.randint(33, 48),
                }[self.profile[customer_id]]

    # ------------------------------------------------------------- operations
    def make_sale(
        self,
        shop_id: str,
        when: datetime,
        customer_id: str | None,
        lines: list[tuple[str, int]],
        payment_mode: str,
        *,
        discount_pct: int = 0,
        by: str = "seed",
    ) -> dict[str, Any]:
        sale_id = self.next_id("SALE", 6)
        subtotal = Decimal("0")
        tax = Decimal("0")
        items = []
        for product_id, qty in lines:
            product = self.products[product_id]
            line_total = money(product["selling_price"] * qty)
            subtotal += line_total
            gst = product["gst_rate"]
            tax += line_total * gst / (Decimal(100) + gst)
            items.append(
                {
                    "sale_id": sale_id,
                    "product_id": product_id,
                    "quantity": qty,
                    "unit_price": product["selling_price"],
                    "line_total": line_total,
                }
            )
        discount = money(subtotal * discount_pct / 100)
        total = subtotal - discount
        if subtotal:
            tax = tax * (total / subtotal)
        sale = {
            "id": sale_id,
            "shop_id": shop_id,
            "customer_id": customer_id,
            "sold_at": when,
            "payment_mode": payment_mode,
            "subtotal": money(subtotal),
            "discount": discount,
            "tax_amount": money(tax),
            "total": money(total),
            "status": "completed",
            "created_at": when,
        }
        self.ds.sales.append(sale)
        self.ds.sale_items.extend(items)
        self.sales_by_id[sale_id] = sale
        self.items_by_sale[sale_id] = items
        for item in items:
            self.move(
                item["product_id"], -item["quantity"], "sale", when, ref=("sale", sale_id), by=by
            )
        if payment_mode == "credit":
            assert customer_id is not None  # noqa: S101 - programming error otherwise
            due_on = when.date() + timedelta(days=CREDIT_DAYS)
            self.ds.credit_ledger.append(
                {
                    "shop_id": shop_id,
                    "customer_id": customer_id,
                    "entry_type": "credit_sale",
                    "amount": sale["total"],
                    "sale_id": sale_id,
                    "entry_date": when.date(),
                    "due_on": due_on,
                    "note": None,
                    "created_by": by,
                    "idempotency_key": None,
                }
            )
            self.open_credit[customer_id].append([sale["total"], due_on])
        return sale

    def record_payment(
        self, customer_id: str, day: date, amount: Decimal, note: str, by: str = "seed"
    ) -> None:
        customer = self.customers[customer_id]
        self.ds.credit_ledger.append(
            {
                "shop_id": customer["shop_id"],
                "customer_id": customer_id,
                "entry_type": "payment",
                "amount": -amount,
                "sale_id": None,
                "entry_date": day,
                "due_on": None,
                "note": note,
                "created_by": by,
                "idempotency_key": None,
            }
        )
        remaining = amount
        queue = self.open_credit[customer_id]
        while remaining > 0 and queue:
            take = min(remaining, queue[0][0])
            queue[0][0] -= take
            remaining -= take
            if queue[0][0] <= 0:
                queue.pop(0)

    def send_reminder(self, customer_id: str, day: date, amount: Decimal, since: date) -> None:
        customer = self.customers[customer_id]
        shop = self.shop_specs[customer["shop_id"]]
        when = self.at(day, 10, 30)
        self.ds.notifications.append(
            {
                "shop_id": shop.id,
                "customer_id": customer_id,
                "supplier_id": None,
                "channel": "whatsapp",
                "recipient": customer["phone"],
                "purpose": "payment_reminder",
                "message": (
                    f"Namaste {customer['name']}, a gentle reminder from {shop.name}: "
                    f"Rs {amount:,.2f} is pending on your account since "
                    f"{since:%d %b %Y}. Please pay when convenient. Thank you."
                ),
                "status": "sent",
                "external_id": f"WA-{len(self.ds.notifications) + 1:06d}",
                "created_at": when,
                "sent_at": when,
                "created_by": self.owner[shop.id],
                "idempotency_key": None,
            }
        )
        self.last_reminder[customer_id] = day

    def place_po(
        self,
        shop_id: str,
        supplier_id: str,
        lines: list[tuple[str, int]],
        day: date,
        *,
        expected: date | None = None,
        status: str = "placed",
        notes: str | None = None,
        by: str | None = None,
    ) -> str:
        po_id = self.next_id("PO", 5)
        supplier = self.suppliers[supplier_id]
        total = Decimal("0")
        for product_id, qty in lines:
            cost = self.products[product_id]["cost_price"]
            total += cost * qty
            item = {
                "purchase_order_id": po_id,
                "product_id": product_id,
                "quantity_ordered": qty,
                "quantity_received": 0,
                "unit_cost": cost,
            }
            self.ds.purchase_order_items.append(item)
            self.po_items[po_id].append(item)
            if status != "draft":
                self.open_po_by_product[product_id] = po_id
        po = {
            "id": po_id,
            "shop_id": shop_id,
            "supplier_id": supplier_id,
            "status": status,
            "ordered_at": None if status == "draft" else self.at(day, 19),
            "expected_on": None
            if status == "draft"
            else (expected or day + timedelta(days=supplier.lead_time_days)),
            "received_at": None,
            "total_amount": money(total),
            "notes": notes,
            "created_by": by or self.owner[shop_id],
            "idempotency_key": None,
            "created_at": self.at(day, 19 if status != "draft" else 11),
        }
        self.ds.purchase_orders.append(po)
        self.pos[po_id] = po
        return po_id

    def receive_po(
        self, po_id: str, day: date, *, short_item: int | None = None, short_ratio: float = 1.0
    ) -> None:
        po = self.pos[po_id]
        when = self.at(day, 10)
        short = False
        for index, item in enumerate(self.po_items[po_id]):
            qty = item["quantity_ordered"]
            if short_item == index:
                qty = max(1, int(qty * short_ratio))
                short = qty < item["quantity_ordered"]
            item["quantity_received"] = qty
            self.move(
                item["product_id"],
                qty,
                "purchase_receipt",
                when,
                ref=("purchase_order", po_id),
                by=po["created_by"],
            )
            self.open_po_by_product.pop(item["product_id"], None)
        po["status"] = "partially_received" if short else "received"
        po["received_at"] = when

    # ------------------------------------------------------------ simulation
    def sellable(self, shop_id: str) -> list[str]:
        return [
            pid
            for pid in self.products_by_shop[shop_id]
            if self.products[pid]["stock_qty"] > 0
            and self.products[pid]["is_active"]
            and pid not in self.no_sales
        ]

    def simulate_day(self, day: date) -> None:
        # Deliveries arrive in the morning.
        for po_id in self.receipts_due.pop(day, []):
            if self.rng.random() < 0.07:
                item_count = len(self.po_items[po_id])
                self.receive_po(
                    po_id,
                    day,
                    short_item=self.rng.randrange(item_count),
                    short_ratio=self.rng.uniform(0.6, 0.9),
                )
                self.sim_events.append({"kind": "short_delivery", "po_id": po_id, "day": day})
            else:
                self.receive_po(po_id, day)

        for spec in SHOPS:
            self.simulate_bills(spec, day)
            self.simulate_damage(spec, day)
            self.simulate_returns(spec, day)

        self.simulate_payments_and_reminders(day)
        # One chance per five shops, so the share of products whose price changes
        # stays the same however many shops there are.
        for _ in range(max(1, len(SHOPS) // 5)):
            if self.rng.random() < 0.13:
                self.simulate_price_change(day)
        for spec in SHOPS:
            self.simulate_reorders(spec, day)

    def simulate_bills(self, spec: ShopSpec, day: date) -> None:
        for _ in range(self.rng.choice(spec.bills_per_day)):
            candidates = self.sellable(spec.id)
            if not candidates:
                return
            customer_id = None
            if self.rng.random() < 0.65:
                customer_id = self.rng.choice(self.customers_by_shop[spec.id])
            customer = self.customers.get(customer_id) if customer_id else None
            contractor = bool(customer and customer["customer_type"] == "contractor")
            weights = [self.demand[pid] for pid in candidates]
            n_items = min(len(candidates), self.rng.choice([1, 1, 2, 2, 3, 4]))
            chosen: list[str] = []
            while len(chosen) < n_items:
                pick = self.rng.choices(candidates, weights=weights)[0]
                if pick not in chosen:
                    chosen.append(pick)
            lines = []
            for pid in chosen:
                qty = self.rng.choice([1, 1, 1, 2, 2, 3])
                if contractor and self.products[pid]["category"] in CONSTRUCTION_CATEGORIES:
                    qty *= self.rng.randint(3, 8)
                qty = min(qty, self.products[pid]["stock_qty"])
                if qty > 0:
                    lines.append((pid, qty))
            if not lines:
                continue
            estimate = sum(self.products[p]["selling_price"] * q for p, q in lines)
            mode = self.rng.choices(["cash", "upi", "card"], weights=[45, 50, 5])[0]
            if (
                customer
                and customer["credit_allowed"]
                and customer["id"] not in self.no_random_credit
                and self.rng.random() < (0.8 if customer["customer_type"] != "household" else 0.45)
                and self.balance(customer["id"]) + estimate <= customer["credit_limit"]
            ):
                mode = "credit"
            discount = 0
            if estimate > 500 and self.rng.random() < 0.15:
                discount = self.rng.choice([2, 3, 5])
            when = self.at(day, self.rng.randint(8, 20), self.rng.choice([0, 10, 20, 30, 40, 50]))
            sale = self.make_sale(
                spec.id,
                when,
                customer_id,
                lines,
                mode,
                discount_pct=discount,
                by=self.staff[spec.id],
            )
            self.recent_sales[spec.id].append(sale["id"])

    def simulate_damage(self, spec: ShopSpec, day: date) -> None:
        chance = 0.3 if spec.shop_type == "dairy_bakery" else 0.02
        if self.rng.random() >= chance:
            return
        pool = [
            pid
            for pid in self.sellable(spec.id)
            if spec.shop_type != "dairy_bakery"
            or self.products[pid]["category"] in PERISHABLE_CATEGORIES
        ]
        if not pool:
            return
        pid = self.rng.choice(pool)
        qty = min(self.rng.randint(1, 3), self.products[pid]["stock_qty"])
        reason = (
            "Expired: past best-before date"
            if spec.shop_type == "dairy_bakery"
            else "Damaged packaging, not sellable"
        )
        self.move(pid, -qty, "damage", self.at(day, 21), reason=reason, by=self.staff[spec.id])

    def simulate_returns(self, spec: ShopSpec, day: date) -> None:
        if self.rng.random() >= 0.02:
            return
        recent = [
            sid
            for sid in self.recent_sales[spec.id][-15:]
            if self.sales_by_id[sid]["payment_mode"] != "credit"
            and self.sales_by_id[sid]["status"] == "completed"
            and (day - self.sales_by_id[sid]["sold_at"].date()).days <= 7
        ]
        if not recent:
            return
        sale_id = self.rng.choice(recent)
        reason = self.rng.choice(
            ["Wrong size bought", "Item defective", "Bought by mistake", "Duplicate purchase"]
        )
        self.return_sale(sale_id, day, reason)
        self.sim_events.append({"kind": "return", "sale_id": sale_id, "day": day, "reason": reason})

    def return_sale(self, sale_id: str, day: date, reason: str) -> None:
        sale = self.sales_by_id[sale_id]
        sale["status"] = "returned"
        for item in self.items_by_sale[sale_id]:
            self.move(
                item["product_id"],
                item["quantity"],
                "customer_return",
                self.at(day, 12),
                ref=("sale", sale_id),
                reason=f"Customer return: {reason}",
                by=self.staff[sale["shop_id"]],
            )

    def simulate_payments_and_reminders(self, day: date) -> None:
        for customer_id in list(self.open_credit):
            queue = self.open_credit[customer_id]
            if not queue or customer_id in self.no_random_credit:
                continue
            oldest_due = queue[0][1]
            age = (day - (oldest_due - timedelta(days=CREDIT_DAYS))).days
            profile = self.profile[customer_id]
            visits = 0.5 if profile == "prompt" else 0.3
            if age >= self.pay_after[customer_id] and self.rng.random() < visits:
                owed = self.balance(customer_id)
                partial = Decimal(str(self.rng.choice([0.5, 0.75, 1])))
                share = Decimal("1") if profile != "slow" else partial
                amount = money(owed * share)
                via = self.rng.choice(["UPI", "cash"])
                self.record_payment(
                    customer_id,
                    day,
                    amount,
                    f"Paid by {via}",
                    by=self.staff[self.customers[customer_id]["shop_id"]],
                )
                continue
            overdue_days = (day - oldest_due).days
            last = self.last_reminder.get(customer_id)
            if (
                overdue_days >= OVERDUE_GRACE_DAYS
                and (last is None or (day - last).days >= REMINDER_GAP_DAYS)
                and self.rng.random() < 0.5
            ):
                since = oldest_due - timedelta(days=CREDIT_DAYS)
                self.send_reminder(customer_id, day, self.balance(customer_id), since)

    def simulate_price_change(self, day: date) -> None:
        candidates = [pid for pid in self.products if pid not in self.no_price_change]
        pid = self.rng.choice(candidates)
        product = self.products[pid]
        shop_id = product["shop_id"]
        bump = Decimal(str(self.rng.choice([1.03, 1.05, 1.06, 1.08])))
        new_cost = money((product["cost_price"] * bump).to_integral_value())
        new_price = money((product["selling_price"] * bump).to_integral_value())
        new_price = min(new_price, product["mrp"])
        when = self.at(day, 17)
        owner = self.owner[shop_id]
        supplier = self.suppliers[product["preferred_supplier_id"]].name
        for column, old, new, reason in (
            ("cost_price", product["cost_price"], new_cost, f"New rate from {supplier} on invoice"),
            ("selling_price", product["selling_price"], new_price, "Passed on supplier increase"),
        ):
            if new == old:
                continue
            self.ds.price_changes.append(
                {
                    "shop_id": shop_id,
                    "product_id": pid,
                    "field": column,
                    "old_value": old,
                    "new_value": new,
                    "reason": reason,
                    "changed_at": when,
                    "changed_by": owner,
                    "approved_by": owner,
                    "idempotency_key": None,
                }
            )
            product[column] = new

    def simulate_reorders(self, spec: ShopSpec, day: date) -> None:
        by_supplier: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for pid in self.products_by_shop[spec.id]:
            product = self.products[pid]
            blocked_from = self.no_reorder_from.get(pid)
            if (
                product["is_active"]
                and product["stock_qty"] <= product["reorder_level"]
                and pid not in self.open_po_by_product
                and pid not in self.no_sales
                and (blocked_from is None or day < blocked_from)
            ):
                by_supplier[product["preferred_supplier_id"]].append((pid, product["reorder_qty"]))
        for supplier_id, lines in by_supplier.items():
            po_id = self.place_po(spec.id, supplier_id, lines, day)
            lead = self.suppliers[supplier_id].lead_time_days
            delay = self.rng.randint(1, 3) if self.rng.random() < 0.12 else 0
            arrival = day + timedelta(days=lead + delay)
            if delay:
                self.sim_events.append(
                    {"kind": "late_delivery", "po_id": po_id, "day": arrival, "delay": delay}
                )
            if arrival <= self.anchor:
                self.receipts_due[arrival].append(po_id)

    def run(self) -> Dataset:
        self.build_reference_data()
        self.build_customers()
        self.reserve_edge_case_entities()
        self.build_opening_stock()
        day = self.start
        while day <= self.anchor:
            self.simulate_day(day)
            day += timedelta(days=1)
        _plant_edge_cases(self)
        _build_cases(self)
        return self.ds

    # ------------------------------------------------------- edge-case setup
    def reserve_edge_case_entities(self) -> None:
        """Keep the simulation away from what the edge cases will set up by hand."""
        a = self.anchor
        rules = {
            ("SHOP-001", "Toor Dal 1kg"): a - timedelta(days=10),
            ("SHOP-004", "Paneer 200g"): a - timedelta(days=10),
            ("SHOP-001", "Sunflower Oil 1L"): a - timedelta(days=10),
            ("SHOP-002", "OPC Cement 50kg bag"): a - timedelta(days=15),
            ("SHOP-003", "A4 Copier Paper 500 sheets"): a - timedelta(days=10),
        }
        for (shop_id, name), blocked_from in rules.items():
            self.no_reorder_from[self.pid(shop_id, name)] = blocked_from
        for shop_id, name in [
            ("SHOP-005", "Selfie Stick"),
            ("SHOP-003", "Correction Pen"),
        ]:
            self.no_sales.add(self.pid(shop_id, name))
        self.no_price_change.update(
            {self.pid("SHOP-004", "Ghee 500ml"), *self.no_reorder_from, *self.no_sales}
        )
        # The first four SHOP-001 customers and the first SHOP-002 contractor get
        # hand-made credit histories, so the simulation must not give them credit.
        for customer_id in self.customers_by_shop["SHOP-001"][:4]:
            self.customers[customer_id]["credit_allowed"] = True
            self.customers[customer_id]["customer_type"] = "household"
            self.no_random_credit.add(customer_id)
        self.edge_contractor = next(
            c
            for c in self.customers_by_shop["SHOP-002"]
            if self.customers[c]["customer_type"] == "contractor"
        )
        self.no_random_credit.add(self.edge_contractor)


def _plant_edge_cases(sim: _Simulator) -> None:
    """Add the hand-made situations, and describe each in ``edge_cases``."""
    a = sim.anchor
    ds = sim.ds
    d = timedelta
    shop1_customers = sim.customers_by_shop["SHOP-001"]

    def lines(shop_id: str, *pairs: tuple[str, int]) -> list[tuple[str, int]]:
        result = []
        for name, qty in pairs:
            pid = sim.pid(shop_id, name)
            qty = min(qty, sim.products[pid]["stock_qty"])
            if qty > 0:
                result.append((pid, qty))
        return result

    def reduce_to(shop_id: str, name: str, target: int, when: datetime) -> str:
        pid = sim.pid(shop_id, name)
        surplus = sim.products[pid]["stock_qty"] - target
        if surplus > 0:
            sim.make_sale(shop_id, when, None, [(pid, surplus)], "cash", by=sim.staff[shop_id])
        return pid

    # 1. Over the credit limit (policy v2 standard limit Rs 3,000).
    over = shop1_customers[0]
    sim.customers[over]["credit_limit"] = money(3000)
    sim.make_sale(
        "SHOP-001",
        sim.at(a - d(20), 11),
        over,
        lines(
            "SHOP-001", ("Sona Masoori Rice 25kg", 1), ("Toor Dal 1kg", 2), ("Sunflower Oil 1L", 2)
        ),
        "credit",
    )
    sim.make_sale(
        "SHOP-001",
        sim.at(a - d(6), 18),
        over,
        lines(
            "SHOP-001",
            ("Wheat Atta 10kg", 2),
            ("Basmati Rice 1kg", 5),
            ("Tea Powder 250g", 2),
            ("Sugar 1kg", 5),
        ),
        "credit",
    )
    ds.edge_cases["over_credit_limit"] = {
        "shop_id": "SHOP-001",
        "customer_id": over,
        "balance": str(sim.balance(over)),
        "credit_limit": "3000.00",
        "expect": "Balance is above the Rs 3,000 limit: block new credit (POL-CREDIT-001 v2).",
    }

    # 2. Overdue for more than 45 days: credit must be blocked, reminders already sent.
    overdue = shop1_customers[1]
    sim.customers[overdue]["credit_limit"] = money(3000)
    sim.make_sale(
        "SHOP-001",
        sim.at(a - d(82), 10),
        overdue,
        lines("SHOP-001", ("Sona Masoori Rice 25kg", 1), ("Groundnut Oil 1L", 1), ("Sugar 1kg", 3)),
        "credit",
    )
    for days_ago in (45, 30, 16):
        due = a - d(82) + d(CREDIT_DAYS)
        sim.send_reminder(overdue, a - d(days_ago), sim.balance(overdue), due - d(CREDIT_DAYS))
    ds.edge_cases["overdue_credit"] = {
        "shop_id": "SHOP-001",
        "customer_id": overdue,
        "balance": str(sim.balance(overdue)),
        "days_overdue": 52,
        "expect": "Dues are 52 days past due: no new credit; escalate to the owner.",
    }

    # 3. Limit set under the old policy (v1 allowed Rs 5,000).
    legacy = shop1_customers[2]
    sim.customers[legacy]["credit_limit"] = money(5000)
    sim.customers[legacy]["joined_on"] = date(2025, 11, 3)
    sim.customers[legacy]["notes"] = "Credit limit set in Nov 2025."
    sim.make_sale(
        "SHOP-001",
        sim.at(a - d(25), 12),
        legacy,
        lines(
            "SHOP-001",
            ("Sona Masoori Rice 25kg", 1),
            ("Moong Dal 1kg", 3),
            ("Detergent Powder 1kg", 2),
        ),
        "credit",
    )
    sim.make_sale(
        "SHOP-001",
        sim.at(a - d(12), 12),
        legacy,
        lines(
            "SHOP-001",
            ("Wheat Atta 10kg", 2),
            ("Groundnut Oil 1L", 3),
            ("Coffee Powder 200g", 2),
            ("Toothpaste 150g", 2),
        ),
        "credit",
    )
    ds.edge_cases["legacy_credit_limit"] = {
        "shop_id": "SHOP-001",
        "customer_id": legacy,
        "balance": str(sim.balance(legacy)),
        "credit_limit": "5000.00",
        "expect": "Rs 5,000 limit comes from policy v1; v2 says Rs 3,000. Flag for owner review.",
    }

    # 4. Overdue, but reminded 2 days ago: do not remind again yet.
    reminded = shop1_customers[3]
    sim.customers[reminded]["credit_limit"] = money(3000)
    sim.make_sale(
        "SHOP-001",
        sim.at(a - d(45), 17),
        reminded,
        lines("SHOP-001", ("Toor Dal 1kg", 2), ("Sugar 1kg", 4), ("Glucose Biscuits 200g", 6)),
        "credit",
    )
    sim.send_reminder(reminded, a - d(2), sim.balance(reminded), a - d(45))
    ds.edge_cases["reminder_sent_recently"] = {
        "shop_id": "SHOP-001",
        "customer_id": reminded,
        "balance": str(sim.balance(reminded)),
        "last_reminder": str(a - d(2)),
        "expect": "Overdue, but a reminder went out 2 days ago: wait until 7 days have passed.",
    }

    # 5. The same UPI payment entered twice for a contractor.
    contractor = sim.edge_contractor
    sim.make_sale(
        "SHOP-002",
        sim.at(a - d(40), 9),
        contractor,
        lines("SHOP-002", ("OPC Cement 50kg bag", 20), ("Wire Nails 2 inch 1kg", 5)),
        "credit",
    )
    for _ in range(2):
        sim.record_payment(
            contractor,
            a - d(9),
            money(6000),
            "Paid by UPI, ref 417893562210",
            by=sim.staff["SHOP-002"],
        )
    ds.edge_cases["duplicate_payment"] = {
        "shop_id": "SHOP-002",
        "customer_id": contractor,
        "payment_ref": "417893562210",
        "expect": "Two Rs 6,000 payments with the same UPI reference on the same day: likely a "
        "duplicate entry. Ask a person to check before changing the ledger.",
    }

    # 6. Two records for the same person.
    shop3 = "SHOP-003"
    phone = sim.phone(3)
    for name, joined in (("Priya Sharma", a - d(400)), ("Priya S.", a - d(20))):
        customer_id = sim.next_id("CUST", 4)
        row = {
            "id": customer_id,
            "shop_id": shop3,
            "name": name,
            "phone": phone,
            "locality": "Teachers Colony",
            "customer_type": "household",
            "credit_allowed": False,
            "credit_limit": money(0),
            "is_active": True,
            "notes": "Added at the counter." if name == "Priya S." else None,
            "joined_on": joined,
            "created_at": sim.at(joined, 16),
        }
        ds.customers.append(row)
        sim.customers[customer_id] = row
        sim.customers_by_shop[shop3].append(customer_id)
    ds.edge_cases["duplicate_customer"] = {
        "shop_id": shop3,
        "customer_ids": sim.customers_by_shop[shop3][-2:],
        "phone": phone,
        "expect": "Same phone number on two customer records: suggest merging, do not merge alone.",
    }

    # 7. Returned item (defective earbuds).
    earbuds = sim.pid("SHOP-005", "Bluetooth Earbuds")
    buyer = sim.customers_by_shop["SHOP-005"][0]
    sale = sim.make_sale(
        "SHOP-005", sim.at(a - d(6), 18), buyer, [(earbuds, 1)], "upi", by=sim.staff["SHOP-005"]
    )
    sim.return_sale(sale["id"], a - d(4), "Left earbud not charging")
    ds.edge_cases["returned_sale"] = {
        "shop_id": "SHOP-005",
        "sale_id": sale["id"],
        "product_id": earbuds,
        "expect": "Returned within 7 days as defective: refund or replace (POL-RETURNS-001).",
    }

    # 8. Stock count lower than records.
    basmati = sim.pid("SHOP-001", "Basmati Rice 1kg")
    shortfall = min(6, sim.products[basmati]["stock_qty"])
    sim.move(
        basmati,
        -shortfall,
        "adjustment",
        sim.at(a - d(2), 20),
        reason=f"Monthly stock count: {shortfall} fewer packets on shelf than in records",
        by=sim.staff["SHOP-001"],
    )
    ds.edge_cases["stock_count_mismatch"] = {
        "shop_id": "SHOP-001",
        "product_id": basmati,
        "missing_units": shortfall,
        "expect": "Adjustment recorded after the count; investigate sales and receipts, open case.",
    }

    # 9. Supplier raised the cost above the selling price.
    ghee = sim.pid("SHOP-004", "Ghee 500ml")
    product = sim.products[ghee]
    ds.price_changes.append(
        {
            "shop_id": "SHOP-004",
            "product_id": ghee,
            "field": "cost_price",
            "old_value": product["cost_price"],
            "new_value": money(352),
            "reason": "New rate from Nandi Milk Co-operative on invoice",
            "changed_at": sim.at(a - d(3), 17),
            "changed_by": sim.owner["SHOP-004"],
            "approved_by": sim.owner["SHOP-004"],
            "idempotency_key": None,
        }
    )
    product["cost_price"] = money(352)
    ds.edge_cases["negative_margin"] = {
        "shop_id": "SHOP-004",
        "product_id": ghee,
        "cost_price": "352.00",
        "selling_price": str(product["selling_price"]),
        "expect": "Selling below cost: propose a price within MRP; price changes need approval.",
    }

    # 10. Low stock and nothing on order.
    for shop_id, name, key in (
        ("SHOP-001", "Toor Dal 1kg", "low_stock_no_po"),
        ("SHOP-004", "Paneer 200g", "low_stock_no_po_dairy"),
    ):
        pid = reduce_to(shop_id, name, 2, sim.at(a, 18))
        assert pid not in sim.open_po_by_product  # noqa: S101
        ds.edge_cases[key] = {
            "shop_id": shop_id,
            "product_id": pid,
            "stock_qty": sim.products[pid]["stock_qty"],
            "reorder_level": sim.products[pid]["reorder_level"],
            "expect": "Below reorder level with no open purchase order: propose a reorder.",
        }

    # 11. Low stock but already on order: do not order twice.
    oil = reduce_to("SHOP-001", "Sunflower Oil 1L", 3, sim.at(a, 18, 30))
    po_oil = sim.place_po("SHOP-001", "SUP-002", [(oil, 30)], a - d(1), expected=a + d(2))
    ds.edge_cases["low_stock_with_open_po"] = {
        "shop_id": "SHOP-001",
        "product_id": oil,
        "purchase_order_id": po_oil,
        "expect": "Already on order, arriving in 2 days: do not create another order.",
    }

    # 12. Supplier late.
    cement = reduce_to("SHOP-002", "OPC Cement 50kg bag", 6, sim.at(a, 17))
    po_cement = sim.place_po("SHOP-002", "SUP-004", [(cement, 80)], a - d(12), expected=a - d(7))
    ds.edge_cases["late_purchase_order"] = {
        "shop_id": "SHOP-002",
        "product_id": cement,
        "purchase_order_id": po_cement,
        "days_late": 7,
        "expect": "Delivery is 7 days late: follow up with the supplier before reordering.",
    }

    # 13. Short delivery.
    paper = sim.pid("SHOP-003", "A4 Copier Paper 500 sheets")
    po_paper = sim.place_po("SHOP-003", "SUP-008", [(paper, 30)], a - d(6), expected=a - d(4))
    sim.receive_po(po_paper, a - d(4), short_item=0, short_ratio=20 / 30)
    ds.edge_cases["short_delivery"] = {
        "shop_id": "SHOP-003",
        "product_id": paper,
        "purchase_order_id": po_paper,
        "ordered": 30,
        "received": 20,
        "expect": "10 reams missing: claim credit from the supplier (SOP-SHORT-001).",
    }

    # 14. Big draft order from staff, waiting for the owner.
    paint = sim.pid("SHOP-002", "Exterior Emulsion Paint 4L")
    po_paint = sim.place_po(
        "SHOP-002",
        "SUP-005",
        [(paint, 10)],
        a,
        status="draft",
        notes="Festival season painting demand",
        by=sim.staff["SHOP-002"],
    )
    ds.edge_cases["high_value_draft_po"] = {
        "shop_id": "SHOP-002",
        "purchase_order_id": po_paint,
        "total": str(sim.pos[po_paint]["total_amount"]),
        "expect": "Orders over Rs 10,000 need the owner's approval (POL-APPROVAL-001).",
    }

    # 15. Dead stock and an inactive product with stock.
    selfie = sim.pid("SHOP-005", "Selfie Stick")
    pen = sim.pid("SHOP-003", "Correction Pen")
    sim.products[pen]["is_active"] = False
    ds.edge_cases["dead_stock"] = {
        "shop_id": "SHOP-005",
        "product_id": selfie,
        "stock_qty": sim.products[selfie]["stock_qty"],
        "expect": "No sales in 90 days: suggest a discount or return to supplier; do not reorder.",
    }
    ds.edge_cases["inactive_product_with_stock"] = {
        "shop_id": "SHOP-003",
        "product_id": pen,
        "stock_qty": sim.products[pen]["stock_qty"],
        "expect": "Marked inactive but still has stock: ask whether to sell off or reactivate.",
    }


CASE_TEMPLATES: list[tuple[str, str, str, str]] = [
    (
        "pricing",
        "Customer says {product} is cheaper at another shop",
        "Customer compared the price of {product} with a nearby shop.",
        "Explained that our price is within MRP; staff gave a 2% discount, within the staff limit.",
    ),
    (
        "customer_complaint",
        "{product} found damaged after purchase",
        "Customer returned the next day saying {product} was damaged.",
        "Replaced the item; damaged unit written off as damage.",
    ),
    (
        "credit_dispute",
        "Customer says a payment was not recorded",
        "{customer} said they paid by UPI but the credit book still showed the amount.",
        "Checked the UPI statement, found the payment and recorded it.",
    ),
    (
        "stock_discrepancy",
        "Count mismatch for {product}",
        "Weekly count of {product} was lower than the records.",
        "Recount found the missing units in the back store. No adjustment needed.",
    ),
    (
        "reorder",
        "Ran out of {product} during a busy week",
        "{product} sold out before the next delivery.",
        "Raised the reorder level for {product} for the festival season.",
    ),
    (
        "supplier_issue",
        "Wrong item delivered by supplier",
        "Delivery from {supplier} had a different brand of {product}.",
        "Supplier exchanged the items on the next visit.",
    ),
]


def _build_cases(sim: _Simulator) -> None:
    """Past cases, about three per shop: real events from the simulation, then templates."""
    ds = sim.ds
    a = sim.anchor
    target = max(50, 3 * len(SHOPS))
    template_reserve = round(target * 0.24)  # room left for template cases (12 of 50)

    def add(
        shop_id: str,
        category: str,
        title: str,
        description: str,
        status: str,
        resolution: str | None,
        opened: datetime,
        resolved: datetime | None,
        **links: str | None,
    ) -> None:
        ds.cases.append(
            {
                "id": sim.next_id("CASE", 4),
                "shop_id": shop_id,
                "category": category,
                "title": title,
                "description": description,
                "status": status,
                "resolution": resolution,
                "customer_id": links.get("customer_id"),
                "supplier_id": links.get("supplier_id"),
                "product_id": links.get("product_id"),
                "opened_at": opened,
                "resolved_at": resolved,
            }
        )

    # Open cases tied to the edge cases.
    e = ds.edge_cases
    add(
        "SHOP-001",
        "stock_discrepancy",
        "Basmati rice count short",
        f"Monthly count found {e['stock_count_mismatch']['missing_units']} fewer packets of "
        "Basmati Rice 1kg than the records.",
        "open",
        None,
        sim.at(a - timedelta(days=2), 20),
        None,
        product_id=e["stock_count_mismatch"]["product_id"],
    )
    add(
        "SHOP-003",
        "supplier_issue",
        "Short delivery of A4 paper",
        "PaperWorld Wholesale delivered 20 of 30 reams ordered.",
        "open",
        None,
        sim.at(a - timedelta(days=4), 11),
        None,
        supplier_id="SUP-008",
        product_id=e["short_delivery"]["product_id"],
    )
    add(
        "SHOP-002",
        "credit_dispute",
        "Possible duplicate UPI payment entry",
        "Two Rs 6,000 payments with UPI ref 417893562210 were entered on the same day.",
        "open",
        None,
        sim.at(a - timedelta(days=8), 10),
        None,
        customer_id=e["duplicate_payment"]["customer_id"],
    )
    add(
        "SHOP-001",
        "credit_dispute",
        "Long-overdue credit balance",
        "Customer has not paid for over 50 days despite three reminders.",
        "open",
        None,
        sim.at(a - timedelta(days=5), 18),
        None,
        customer_id=e["overdue_credit"]["customer_id"],
    )
    add(
        "SHOP-005",
        "returns",
        "Earbuds returned as defective",
        "Customer returned Bluetooth Earbuds: left earbud not charging.",
        "open",
        None,
        sim.at(a - timedelta(days=4), 12),
        None,
        product_id=e["returned_sale"]["product_id"],
    )
    add(
        "SHOP-002",
        "supplier_issue",
        "Cement delivery overdue",
        "Deccan Hardware Distributors has not delivered the cement order due a week ago.",
        "open",
        None,
        sim.at(a - timedelta(days=1), 9),
        None,
        supplier_id="SUP-004",
        product_id=e["late_purchase_order"]["product_id"],
    )

    # Resolved cases from things that happened in the simulation.
    for event in sim.sim_events:
        if len(ds.cases) >= target - template_reserve:
            break
        day: date = event["day"]
        if event["kind"] == "short_delivery":
            po = sim.pos[event["po_id"]]
            supplier = sim.suppliers[po["supplier_id"]].name
            add(
                po["shop_id"],
                "supplier_issue",
                f"Short delivery on {po['id']}",
                f"{supplier} delivered fewer units than ordered on {po['id']}.",
                "resolved",
                "Supplier issued a credit note for the missing units against the next invoice "
                "(SOP-SHORT-001).",
                sim.at(day, 11),
                sim.at(day + timedelta(days=3), 16),
                supplier_id=po["supplier_id"],
            )
        elif event["kind"] == "late_delivery":
            po = sim.pos[event["po_id"]]
            supplier = sim.suppliers[po["supplier_id"]].name
            add(
                po["shop_id"],
                "supplier_issue",
                f"Late delivery on {po['id']}",
                f"{supplier} delivered {po['id']} {event['delay']} day(s) late.",
                "resolved",
                "Called the supplier; delivery arrived. Noted for supplier review.",
                sim.at(day, 9),
                sim.at(day, 17),
                supplier_id=po["supplier_id"],
            )
        elif event["kind"] == "return":
            sale = sim.sales_by_id[event["sale_id"]]
            add(
                sale["shop_id"],
                "returns",
                f"Return on {sale['id']}",
                f"Customer returned items from {sale['id']}: {event['reason'].lower()}.",
                "resolved",
                "Refunded within 7 days as per POL-RETURNS-001.",
                sim.at(day, 12),
                sim.at(day, 13),
                customer_id=sale["customer_id"],
            )

    # Fill the rest from templates, spread over the past year. Each one goes to the
    # shop with the fewest cases so far, so every shop has some history to look up.
    case_counts = Counter(case["shop_id"] for case in ds.cases)
    while len(ds.cases) < target:
        spec = min(SHOPS, key=lambda shop: (case_counts[shop.id], shop.id))
        case_counts[spec.id] += 1
        category, title, description, resolution = sim.rng.choice(CASE_TEMPLATES)
        pid = sim.rng.choice(sim.products_by_shop[spec.id])
        product = sim.products[pid]
        customer_id = sim.rng.choice(sim.customers_by_shop[spec.id])
        supplier_id = product["preferred_supplier_id"]
        values = {
            "product": product["name"],
            "customer": sim.customers[customer_id]["name"],
            "supplier": sim.suppliers[supplier_id].name,
        }
        opened_day = a - timedelta(days=sim.rng.randint(20, 360))
        add(
            spec.id,
            category,
            title.format(**values),
            description.format(**values),
            "resolved",
            resolution.format(**values),
            sim.at(opened_day, sim.rng.randint(9, 19)),
            sim.at(opened_day + timedelta(days=sim.rng.randint(0, 4)), 18),
            customer_id=customer_id
            if category in {"credit_dispute", "pricing", "customer_complaint"}
            else None,
            supplier_id=supplier_id if category == "supplier_issue" else None,
            product_id=pid if category != "credit_dispute" else None,
        )


def generate(
    seed: int = DEFAULT_SEED, anchor: date = DEFAULT_ANCHOR, days: int = DEFAULT_DAYS
) -> Dataset:
    """Build the full synthetic dataset. Same arguments, same data."""
    return _Simulator(seed, anchor, days).run()


def validate(ds: Dataset) -> list[str]:
    """Return a list of consistency problems (empty when the data is coherent)."""
    problems: list[str] = []
    totals: dict[str, int] = defaultdict(int)
    for movement in ds.stock_movements:
        totals[movement["product_id"]] += movement["quantity"]
    for product in ds.products:
        if product["stock_qty"] != totals[product["id"]]:
            problems.append(f"{product['id']}: stock_qty differs from its movements")
        if product["stock_qty"] < 0:
            problems.append(f"{product['id']}: negative stock")
    for table in (
        "shops",
        "suppliers",
        "users",
        "customers",
        "products",
        "sales",
        "purchase_orders",
        "cases",
    ):
        ids = [row["id"] for row in ds.rows(table)]
        if len(ids) != len(set(ids)):
            problems.append(f"{table}: duplicate IDs")
    sale_totals: dict[str, Decimal] = defaultdict(Decimal)
    for item in ds.sale_items:
        sale_totals[item["sale_id"]] += item["line_total"]
    for sale in ds.sales:
        if money(sale_totals[sale["id"]]) != sale["subtotal"]:
            problems.append(f"{sale['id']}: items do not add up to the subtotal")
        if sale["subtotal"] - sale["discount"] != sale["total"]:
            problems.append(f"{sale['id']}: total is not subtotal minus discount")
    problems.extend(_shop_boundary_problems(ds))
    return problems


def _shop_boundary_problems(ds: Dataset) -> list[str]:
    """Records must never mix shops: a bill, an order or a ledger entry belongs to one shop."""
    problems: list[str] = []
    shop_of_customer = {c["id"]: c["shop_id"] for c in ds.customers}
    shop_of_product = {p["id"]: p["shop_id"] for p in ds.products}
    shop_of_sale = {s["id"]: s["shop_id"] for s in ds.sales}
    shop_of_order = {po["id"]: po["shop_id"] for po in ds.purchase_orders}

    for sale in ds.sales:
        customer_id = sale["customer_id"]
        if customer_id and shop_of_customer.get(customer_id) != sale["shop_id"]:
            problems.append(f"{sale['id']}: the customer belongs to another shop")
    for item in ds.sale_items:
        if shop_of_product.get(item["product_id"]) != shop_of_sale.get(item["sale_id"]):
            problems.append(f"{item['sale_id']}: sells a product of another shop")
    for order_item in ds.purchase_order_items:
        order_shop = shop_of_order.get(order_item["purchase_order_id"])
        if shop_of_product.get(order_item["product_id"]) != order_shop:
            problems.append(f"{order_item['purchase_order_id']}: orders a product of another shop")
    for entry in ds.credit_ledger:
        if shop_of_customer.get(entry["customer_id"]) != entry["shop_id"]:
            problems.append(f"credit ledger entry of {entry['customer_id']}: wrong shop")
    for movement in ds.stock_movements:
        if shop_of_product.get(movement["product_id"]) != movement["shop_id"]:
            problems.append(f"stock movement of {movement['product_id']}: wrong shop")
    shops_with_sales = set(shop_of_sale.values())
    problems.extend(
        f"{shop['id']}: no sales in the whole period"
        for shop in ds.shops
        if shop["id"] not in shops_with_sales
    )
    return problems
