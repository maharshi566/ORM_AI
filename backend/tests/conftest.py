"""Shared test fixtures.

* API tests: ``settings``, ``app``, ``client`` and patched health checks.
* Tool tests: a seeded SQLite database (built once per session, copied for each
  test so every test starts from identical records), a ``registry`` and
  ``make_ctx``, which builds a ToolContext for a given shop, approval and time.
"""

import asyncio
import shutil
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import Settings
from app.main import create_app
from app.models import Base
from app.seed.generator import Dataset, generate
from app.seed.loader import load_dataset
from app.tools.api_tools import FaultInjector, MockMessagingAPI, MockSupplierAPI
from app.tools.base import IST, ApprovalGrant, ToolContext
from app.tools.registry import ToolRegistry

# ------------------------------------------------------------------ API tests


@pytest.fixture
def settings() -> Settings:
    # _env_file=None: tests never read your local .env file.
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="postgresql+asyncpg://test:test@localhost:5432/orm_ai_test",
        redis_url="redis://localhost:6379/15",
        cors_origins="http://localhost:3000",
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI):
    with TestClient(app) as test_client:
        yield test_client


async def _healthy() -> None:
    return None


async def _down() -> None:
    raise ConnectionError("connection refused")


@pytest.fixture
def all_dependencies_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.health.check_database", _healthy)
    monkeypatch.setattr("app.services.health.check_redis", _healthy)


@pytest.fixture
def redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.health.check_database", _healthy)
    monkeypatch.setattr("app.services.health.check_redis", _down)


# ----------------------------------------------------------------- tool tests

ANCHOR_NOON = datetime(2026, 9, 30, 12, 0, tzinfo=IST)  # "today" in the synthetic data
STAFF = ApprovalGrant(approved_by="USR-002", role="staff", approval_id="test-staff")
OWNER = ApprovalGrant(approved_by="USR-001", role="owner", approval_id="test-owner")


async def _build_seeded_db(path: Path, dataset: Dataset) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await load_dataset(engine, dataset, reset=False)
    await engine.dispose()


@pytest.fixture(scope="session")
def seed_data() -> Dataset:
    return generate()


@pytest.fixture(scope="session")
def seeded_template(tmp_path_factory: pytest.TempPathFactory, seed_data: Dataset) -> Path:
    path = tmp_path_factory.mktemp("db") / "template.db"
    asyncio.run(_build_seeded_db(path, seed_data))
    return path


@pytest.fixture
async def session_factory(
    seeded_template: Path, tmp_path: Path
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    path = tmp_path / "shop.db"
    shutil.copy(seeded_template, path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry(retry_backoff_seconds=0)


@pytest.fixture
async def make_ctx(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Callable[..., ToolContext]]:
    sessions: list[AsyncSession] = []

    def factory(
        shop_id: str = "SHOP-001",
        *,
        approval: ApprovalGrant | None = None,
        now: datetime = ANCHOR_NOON,
        faults: FaultInjector | None = None,
    ) -> ToolContext:
        session = session_factory()
        sessions.append(session)
        return ToolContext(
            session=session,
            shop_id=shop_id,
            actor="action",
            now=now,
            approval=approval,
            log_session_factory=session_factory,
            clients={
                "supplier_api": MockSupplierAPI(faults),
                "messaging_api": MockMessagingAPI(faults),
            },
        )

    yield factory
    for session in sessions:
        await session.close()
