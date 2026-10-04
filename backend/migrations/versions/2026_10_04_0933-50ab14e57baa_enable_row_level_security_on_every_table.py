"""enable row level security on every table

Supabase exposes every table in the public schema through its Data API. With RLS
enabled and no policies, that API can read nothing, while this app, which connects
as the table owner, is unaffected (owners bypass RLS unless it is FORCEd).
On plain PostgreSQL this is harmless. Any new table needs the same in its migration.

Revision ID: 50ab14e57baa
Revises: e94035f52d58
Create Date: 2026-10-04 09:33:41.924408

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "50ab14e57baa"
down_revision: str | Sequence[str] | None = "e94035f52d58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLES = [
    "shops",
    "suppliers",
    "users",
    "customers",
    "products",
    "sales",
    "sale_items",
    "purchase_orders",
    "purchase_order_items",
    "stock_movements",
    "credit_ledger",
    "price_changes",
    "notifications",
    "cases",
    "sessions",
    "workflows",
    "messages",
    "agent_runs",
    "tool_calls",
    "documents",
    "document_chunks",
    "approvals",
    "audit_logs",
    "evaluations",
    "alembic_version",
]


def _set_rls(enabled: bool) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    action = "ENABLE" if enabled else "DISABLE"
    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" {action} ROW LEVEL SECURITY')


def upgrade() -> None:
    _set_rls(True)


def downgrade() -> None:
    _set_rls(False)
