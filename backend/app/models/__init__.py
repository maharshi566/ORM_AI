"""Importing this package registers every table on ``Base.metadata``.

Alembic and the seed script import it so they see the complete schema.
"""

from app.models.database import Base
from app.models.platform import (
    AgentRun,
    Approval,
    AuditLog,
    ChatSession,
    Document,
    DocumentChunk,
    Evaluation,
    Message,
    ToolCall,
    User,
    Workflow,
)
from app.models.shop import (
    Case,
    CreditEntry,
    Customer,
    Notification,
    PriceChange,
    Product,
    PurchaseOrder,
    PurchaseOrderItem,
    Sale,
    SaleItem,
    Shop,
    StockMovement,
    Supplier,
)

__all__ = [
    "AgentRun",
    "Approval",
    "AuditLog",
    "Base",
    "Case",
    "ChatSession",
    "CreditEntry",
    "Customer",
    "Document",
    "DocumentChunk",
    "Evaluation",
    "Message",
    "Notification",
    "PriceChange",
    "Product",
    "PurchaseOrder",
    "PurchaseOrderItem",
    "Sale",
    "SaleItem",
    "Shop",
    "StockMovement",
    "Supplier",
    "ToolCall",
    "User",
    "Workflow",
]
