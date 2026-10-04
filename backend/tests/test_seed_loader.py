from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.models import Base, Product, StockMovement
from app.seed.generator import Dataset, generate
from app.seed.loader import database_has_shop_data, edge_cases_markdown, export_csv, load_dataset


@pytest.fixture
async def engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    db_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'seed.db'}")
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield db_engine
    await db_engine.dispose()


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return generate()


async def test_load_writes_every_table(engine: AsyncEngine, dataset: Dataset) -> None:
    assert not await database_has_shop_data(engine)

    await load_dataset(engine, dataset, reset=False)

    async with engine.connect() as conn:
        for table, expected in dataset.counts().items():
            actual = await conn.scalar(text(f"SELECT COUNT(*) FROM {table}"))  # noqa: S608
            assert actual == expected, table
    assert await database_has_shop_data(engine)


async def test_reset_reload_gives_the_same_rows(engine: AsyncEngine, dataset: Dataset) -> None:
    await load_dataset(engine, dataset, reset=False)
    await load_dataset(engine, dataset, reset=True)

    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT COUNT(*) FROM sales")) == len(dataset.sales)


async def test_stock_matches_movements_in_db(engine: AsyncEngine, dataset: Dataset) -> None:
    await load_dataset(engine, dataset, reset=False)

    movement_total = (
        select(func.coalesce(func.sum(StockMovement.quantity), 0))
        .where(StockMovement.product_id == Product.id)
        .scalar_subquery()
    )
    async with engine.connect() as conn:
        mismatches = await conn.scalar(
            select(func.count()).select_from(Product).where(Product.stock_qty != movement_total)
        )
    assert mismatches == 0


def test_csv_export_and_edge_case_report(tmp_path, dataset: Dataset) -> None:
    files = export_csv(dataset, tmp_path)

    assert (tmp_path / "products.csv").read_text().startswith("id,shop_id,sku,name")
    assert len(files) == len(dataset.counts()) + 1
    report = edge_cases_markdown(dataset)
    assert "`over_credit_limit`" in report and "`duplicate_payment`" in report
