"""Write tools on a real PostgreSQL (skipped unless TEST_DATABASE_URL is set; CI sets it).

SQLite and PostgreSQL behave differently when an insert breaks a unique rule: on
PostgreSQL the whole transaction is then refused until it is rolled back. The tools
insert new record numbers inside a savepoint so that only that one insert is undone;
these tests check that on the real database, including requests that really run at
the same time.
"""

import asyncio
import os

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Case, PurchaseOrder, PurchaseOrderItem
from app.seed.loader import load_dataset
from app.tools import business_tools
from app.tools.api_tools import FailureMode, FaultInjector, MockMessagingAPI, MockSupplierAPI
from app.tools.base import ToolContext, ToolError
from tests.conftest import ANCHOR_NOON, STAFF

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
SCHEMA = "tools_test"

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")


@pytest.fixture
async def pg_factory(seed_data):
    """The synthetic shops in a schema of their own, dropped afterwards."""
    admin = create_async_engine(TEST_DATABASE_URL)
    async with admin.begin() as conn:
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        await conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    engine = create_async_engine(
        TEST_DATABASE_URL, connect_args={"server_settings": {"search_path": SCHEMA}}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await load_dataset(engine, seed_data, reset=False)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
    async with admin.begin() as conn:
        await conn.execute(text(f"DROP SCHEMA {SCHEMA} CASCADE"))
    await admin.dispose()


@pytest.fixture
async def pg_ctx(pg_factory):
    sessions = []

    def factory(shop_id: str = "SHOP-001", *, approval=None, faults=None) -> ToolContext:  # type: ignore[no-untyped-def]
        session = pg_factory()
        sessions.append(session)
        return ToolContext(
            session=session,
            shop_id=shop_id,
            actor="action",
            now=ANCHOR_NOON,
            approval=approval,
            log_session_factory=pg_factory,
            clients={
                "supplier_api": MockSupplierAPI(faults),
                "messaging_api": MockMessagingAPI(faults),
            },
        )

    yield factory
    for session in sessions:
        await session.close()


async def test_a_taken_number_is_skipped_on_postgresql(
    registry, pg_factory, pg_ctx, seed_data, monkeypatch
) -> None:
    async with pg_factory() as session:
        taken = await session.scalar(select(func.max(PurchaseOrder.id)))
    real = business_tools._next_id
    calls: list[int] = []

    async def racing(ctx, column, prefix, width, skip=0):  # type: ignore[no-untyped-def]
        calls.append(skip)
        return taken if len(calls) == 1 else await real(ctx, column, prefix, width, skip=skip)

    monkeypatch.setattr(business_tools, "_next_id", racing)
    toor = seed_data.edge_cases["low_stock_no_po"]["product_id"]
    result = await registry.call(
        "create_purchase_order",
        {
            "supplier_id": "SUP-002",
            "lines": [{"product_id": toor, "quantity": 24}],
            "submit_to_supplier": False,
            "idempotency_key": "pg:race:po-number",
        },
        pg_ctx(),
        agent="action",
    )

    assert result.ok, result.error_message
    new_id = result.data["purchase_order_id"]
    assert new_id > taken
    async with pg_factory() as session:
        lines = await session.scalar(
            select(func.count())
            .select_from(PurchaseOrderItem)
            .where(PurchaseOrderItem.purchase_order_id == new_id)
        )
    assert lines == 1


async def test_cases_opened_at_the_same_moment_get_different_numbers(
    registry, pg_factory, pg_ctx
) -> None:
    """Eight requests open a case at once, each on its own connection."""

    async def open_case(n: int):  # type: ignore[no-untyped-def]
        return await registry.call(
            "create_case",
            {
                "category": "supplier_issue",
                "title": f"Concurrent case {n}",
                "description": "Opened at the same moment as seven others.",
            },
            pg_ctx(),
            agent="action",
        )

    results = await asyncio.gather(*(open_case(n) for n in range(8)))

    assert all(r.ok for r in results), [r.error_message for r in results]
    ids = [r.data["case_id"] for r in results]
    assert len(set(ids)) == 8
    async with pg_factory() as session:
        saved = await session.scalar(
            select(func.count()).select_from(Case).where(Case.title.like("Concurrent case %"))
        )
    assert saved == 8


async def test_a_failed_action_saves_nothing_on_postgresql(registry, pg_factory, pg_ctx, seed_data):
    """The savepoint must not commit early: when the supplier is down after the order
    was inserted, the whole order is undone."""
    ctx = pg_ctx(approval=STAFF, faults=FaultInjector(FailureMode.SERVER_ERROR))
    toor = seed_data.edge_cases["low_stock_no_po"]["product_id"]
    result = await registry.call(
        "create_purchase_order",
        {
            "supplier_id": "SUP-002",
            "lines": [{"product_id": toor, "quantity": 24}],
            "submit_to_supplier": True,
            "idempotency_key": "pg:supplier-down",
        },
        ctx,
        agent="action",
    )

    assert result.error_code == "upstream_error"
    async with pg_factory() as session:
        saved = await session.scalar(
            select(PurchaseOrder).where(PurchaseOrder.idempotency_key == "pg:supplier-down")
        )
    assert saved is None


async def test_gives_up_after_a_few_taken_numbers(registry, pg_ctx, monkeypatch) -> None:
    first = await registry.call(
        "create_case",
        {"category": "reorder", "title": "Race: first", "description": "Opened first."},
        pg_ctx(),
        agent="action",
    )

    async def always_taken(ctx, column, prefix, width, skip=0):  # type: ignore[no-untyped-def]
        return first.data["case_id"]

    monkeypatch.setattr(business_tools, "_next_id", always_taken)
    result = await registry.call(
        "create_case",
        {"category": "reorder", "title": "Race: second", "description": "All taken."},
        pg_ctx(),
        agent="action",
    )

    assert result.error_code == "conflict" and result.retryable
    assert str(business_tools.MAX_ID_ATTEMPTS) in result.error_message


async def test_a_duplicate_that_is_not_the_number_is_not_retried(
    pg_factory, pg_ctx, monkeypatch
) -> None:
    """Two identical requests can both pass the "already done?" check. The second then
    breaks the idempotency-key rule, not the number rule: trying other numbers would
    not help, so it stops at once with a conflict (a retry replays the first order)."""
    async with pg_factory() as session:
        existing = await session.scalar(select(PurchaseOrder).limit(1))
    calls: list[int] = []
    real = business_tools._next_id

    async def counting(ctx, column, prefix, width, skip=0):  # type: ignore[no-untyped-def]
        calls.append(skip)
        return await real(ctx, column, prefix, width, skip=skip)

    monkeypatch.setattr(business_tools, "_next_id", counting)
    ctx = pg_ctx(existing.shop_id)

    def same_key(number: str) -> PurchaseOrder:
        return PurchaseOrder(
            id=number,
            shop_id=existing.shop_id,
            supplier_id=existing.supplier_id,
            status="draft",
            total_amount=existing.total_amount,
            created_by="action",
            idempotency_key=existing.idempotency_key or "pg:dup-key",
            created_at=ANCHOR_NOON,
        )

    if existing.idempotency_key is None:
        async with pg_factory() as session:
            row = await session.get(PurchaseOrder, existing.id)
            row.idempotency_key = "pg:dup-key"
            await session.commit()

    with pytest.raises(ToolError) as caught:
        await business_tools._add_numbered(ctx, PurchaseOrder.id, "PO", 5, same_key)

    assert caught.value.code == "conflict" and caught.value.retryable
    assert len(calls) == 1
