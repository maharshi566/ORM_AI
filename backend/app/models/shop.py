"""Shop records: the business data ORM_AI keeps organised.

Every row belongs to one shop (``shop_id``), so several shops share one database
without seeing each other's records. Suppliers are the exception: one supplier
list is shared by all shops.

How the main pieces connect:

* A **sale** has sale items; each item takes stock out of a **product**.
* A **purchase order** to a **supplier** has items; receiving it puts stock back.
* Every stock change is a row in **stock_movements**, so ``products.stock_qty``
  always equals the sum of that product's movements.
* Buying on credit (udhaar) adds a row to **credit_ledger**; a payment adds a
  negative row. A customer's balance is the sum of their rows.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.database import Base
from app.models.types import (
    CaseCategory,
    CaseStatus,
    CreditEntryType,
    Money,
    MovementType,
    NotificationChannel,
    NotificationStatus,
    PaymentMode,
    PurchaseOrderStatus,
    SaleStatus,
    ShopType,
    enum_type,
    utcnow,
)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow)


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # SHOP-001
    name: Mapped[str] = mapped_column(String(120))
    shop_type: Mapped[ShopType] = mapped_column(enum_type(ShopType, "shop_type"))
    owner_name: Mapped[str] = mapped_column(String(80))
    phone: Mapped[str] = mapped_column(String(20))
    locality: Mapped[str] = mapped_column(String(80))
    city: Mapped[str] = mapped_column(String(60))
    opened_on: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = _created_at()


class Supplier(Base):
    """Wholesalers and distributors. Shared by all shops."""

    __tablename__ = "suppliers"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # SUP-001
    name: Mapped[str] = mapped_column(String(120))
    contact_person: Mapped[str] = mapped_column(String(80))
    phone: Mapped[str] = mapped_column(String(20))
    city: Mapped[str] = mapped_column(String(60))
    categories: Mapped[str] = mapped_column(String(200))  # comma-separated
    payment_terms_days: Mapped[int] = mapped_column(Integer)
    lead_time_days: Mapped[int] = mapped_column(Integer)
    min_order_value: Mapped[Decimal] = mapped_column(Money)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created_at()


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (Index("ix_customers_shop_phone", "shop_id", "phone"),)

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # CUST-0001
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    phone: Mapped[str] = mapped_column(String(20))
    locality: Mapped[str] = mapped_column(String(80))
    customer_type: Mapped[str] = mapped_column(String(20), default="household")
    credit_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    credit_limit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    joined_on: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = _created_at()


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("shop_id", "sku", name="uq_products_shop_sku"),)

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # PRD-0001
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    sku: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(60))
    unit: Mapped[str] = mapped_column(String(20))  # piece, packet, bag, ...
    cost_price: Mapped[Decimal] = mapped_column(Money)  # what the shop pays
    selling_price: Mapped[Decimal] = mapped_column(Money)  # what the customer pays
    mrp: Mapped[Decimal] = mapped_column(Money)  # maximum retail price
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))  # percent, included in price
    stock_qty: Mapped[int] = mapped_column(Integer, default=0)
    reorder_level: Mapped[int] = mapped_column(Integer)  # reorder at or below this
    reorder_qty: Mapped[int] = mapped_column(Integer)  # usual order size
    preferred_supplier_id: Mapped[str | None] = mapped_column(ForeignKey("suppliers.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created_at()


class Sale(Base):
    """One bill."""

    __tablename__ = "sales"
    __table_args__ = (Index("ix_sales_shop_sold_at", "shop_id", "sold_at"),)

    id: Mapped[str] = mapped_column(String(20), primary_key=True)  # SALE-000001
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"))
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id"), index=True)
    sold_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payment_mode: Mapped[PaymentMode] = mapped_column(enum_type(PaymentMode, "payment_mode"))
    subtotal: Mapped[Decimal] = mapped_column(Money)
    discount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    tax_amount: Mapped[Decimal] = mapped_column(Money)  # GST included in total
    total: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[SaleStatus] = mapped_column(enum_type(SaleStatus, "sale_status"))
    created_at: Mapped[datetime] = _created_at()

    items: Mapped[list["SaleItem"]] = relationship(
        back_populates="sale", lazy="selectin", order_by="SaleItem.id"
    )


class SaleItem(Base):
    __tablename__ = "sale_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sale_id: Mapped[str] = mapped_column(ForeignKey("sales.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Money)
    line_total: Mapped[Decimal] = mapped_column(Money)

    sale: Mapped[Sale] = relationship(back_populates="items")


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # PO-00001
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("suppliers.id"), index=True)
    status: Mapped[PurchaseOrderStatus] = mapped_column(enum_type(PurchaseOrderStatus, "po_status"))
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_on: Mapped[date | None] = mapped_column(Date)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_amount: Mapped[Decimal] = mapped_column(Money)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(40))  # user ID, "agent" or "seed"
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True)
    created_at: Mapped[datetime] = _created_at()

    items: Mapped[list["PurchaseOrderItem"]] = relationship(
        back_populates="purchase_order", lazy="selectin", order_by="PurchaseOrderItem.id"
    )


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    purchase_order_id: Mapped[str] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    quantity_ordered: Mapped[int] = mapped_column(Integer)
    quantity_received: Mapped[int] = mapped_column(Integer, default=0)
    unit_cost: Mapped[Decimal] = mapped_column(Money)

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="items")


class StockMovement(Base):
    """Every change to stock. Positive adds stock, negative removes it."""

    __tablename__ = "stock_movements"
    __table_args__ = (Index("ix_stock_movements_product_moved_at", "product_id", "moved_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    movement_type: Mapped[MovementType] = mapped_column(enum_type(MovementType, "movement_type"))
    quantity: Mapped[int] = mapped_column(Integer)
    reference_type: Mapped[str | None] = mapped_column(String(30))  # sale, purchase_order
    reference_id: Mapped[str | None] = mapped_column(String(30))
    reason: Mapped[str | None] = mapped_column(Text)
    moved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True)


class CreditEntry(Base):
    """The customer credit book (khata). Positive = customer owes more."""

    __tablename__ = "credit_ledger"
    __table_args__ = (Index("ix_credit_ledger_customer_date", "customer_id", "entry_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    entry_type: Mapped[CreditEntryType] = mapped_column(enum_type(CreditEntryType, "credit_entry"))
    amount: Mapped[Decimal] = mapped_column(Money)
    sale_id: Mapped[str | None] = mapped_column(ForeignKey("sales.id"))
    entry_date: Mapped[date] = mapped_column(Date)
    due_on: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True)


class PriceChange(Base):
    __tablename__ = "price_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    field: Mapped[str] = mapped_column(String(20))  # selling_price or cost_price
    old_value: Mapped[Decimal] = mapped_column(Money)
    new_value: Mapped[Decimal] = mapped_column(Money)
    reason: Mapped[str] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    changed_by: Mapped[str] = mapped_column(String(40))
    approved_by: Mapped[str | None] = mapped_column(String(40))
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True)


class Notification(Base):
    """Messages sent to customers or suppliers (payment reminders, orders)."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id"), index=True)
    supplier_id: Mapped[str | None] = mapped_column(ForeignKey("suppliers.id"))
    channel: Mapped[NotificationChannel] = mapped_column(enum_type(NotificationChannel, "channel"))
    recipient: Mapped[str] = mapped_column(String(20))
    purpose: Mapped[str] = mapped_column(String(40))  # payment_reminder, purchase_order
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[NotificationStatus] = mapped_column(
        enum_type(NotificationStatus, "notification_status")
    )
    external_id: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True)


class Case(Base):
    """Past problems and how they were resolved: the shop's memory of decisions."""

    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # CASE-0001
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    category: Mapped[CaseCategory] = mapped_column(enum_type(CaseCategory, "case_category"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[CaseStatus] = mapped_column(enum_type(CaseStatus, "case_status"))
    resolution: Mapped[str | None] = mapped_column(Text)
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id"), index=True)
    supplier_id: Mapped[str | None] = mapped_column(ForeignKey("suppliers.id"))
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id"))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
