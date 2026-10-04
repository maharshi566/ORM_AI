"""Column types and enums shared by every model.

All types work on PostgreSQL (the real database) and SQLite (used by fast unit
tests), so the same models run in both places.
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, Numeric
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB

# JSON on SQLite, JSONB (indexable, binary) on PostgreSQL.
JSONType = JSON().with_variant(JSONB(), "postgresql")

# Rupees and paise. Never store money as float.
Money = Numeric(12, 2)


def utcnow() -> datetime:
    return datetime.now(UTC)


def enum_type(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """Store an enum as VARCHAR plus a CHECK constraint (no native Postgres enum).

    Adding a value later is then an ordinary migration instead of an ALTER TYPE.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


# --- Shop domain -----------------------------------------------------------


class ShopType(StrEnum):
    KIRANA = "kirana"
    HARDWARE = "hardware"
    STATIONERY = "stationery"
    DAIRY_BAKERY = "dairy_bakery"
    MOBILE_ACCESSORIES = "mobile_accessories"


class PaymentMode(StrEnum):
    CASH = "cash"
    UPI = "upi"
    CARD = "card"
    CREDIT = "credit"  # bought on credit (udhaar)


class SaleStatus(StrEnum):
    COMPLETED = "completed"
    RETURNED = "returned"
    CANCELLED = "cancelled"


class PurchaseOrderStatus(StrEnum):
    DRAFT = "draft"  # proposed, not sent to the supplier
    PLACED = "placed"  # sent to the supplier, waiting for delivery
    PARTIALLY_RECEIVED = "partially_received"
    RECEIVED = "received"
    CANCELLED = "cancelled"


class MovementType(StrEnum):
    OPENING = "opening"  # stock on hand when records began
    PURCHASE_RECEIPT = "purchase_receipt"
    SALE = "sale"
    CUSTOMER_RETURN = "customer_return"
    ADJUSTMENT = "adjustment"  # physical count correction
    DAMAGE = "damage"  # expired, broken or spoiled


class CreditEntryType(StrEnum):
    CREDIT_SALE = "credit_sale"  # customer owes more
    PAYMENT = "payment"  # customer paid back
    ADJUSTMENT = "adjustment"
    WRITE_OFF = "write_off"


class CaseCategory(StrEnum):
    STOCK_DISCREPANCY = "stock_discrepancy"
    SUPPLIER_ISSUE = "supplier_issue"
    CREDIT_DISPUTE = "credit_dispute"
    PRICING = "pricing"
    CUSTOMER_COMPLAINT = "customer_complaint"
    REORDER = "reorder"
    RETURNS = "returns"


class CaseStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class NotificationChannel(StrEnum):
    SMS = "sms"
    WHATSAPP = "whatsapp"


class NotificationStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"


# --- Platform (the AI app itself) ------------------------------------------


class UserRole(StrEnum):
    OWNER = "owner"
    STAFF = "staff"
    ADMIN = "admin"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class WorkflowStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class RunStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"  # approved with changes


class ActorType(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
