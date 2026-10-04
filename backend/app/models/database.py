"""PostgreSQL access with async SQLAlchemy.

The engine is created at startup but does not connect until first used, so the
API still starts (and reports ``degraded`` health) when Postgres is down.
Tables are defined in app/models/shop.py and app/models/platform.py and created
by Alembic migrations (backend/migrations/).
"""

import uuid
from collections.abc import AsyncIterator

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

# Predictable constraint names, so Alembic can find and change them later.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def make_engine(
    database_url: str, *, transaction_pooler: bool = False, echo: bool = False
) -> AsyncEngine:
    """Create an engine. ``transaction_pooler`` adapts it to PgBouncer-style poolers.

    Transaction-mode poolers (for example Supabase on port 6543) hand each
    transaction a different server connection, so asyncpg's prepared statements
    must be switched off and the app must not keep its own connection pool.
    """
    if transaction_pooler:
        return create_async_engine(
            database_url,
            echo=echo,
            poolclass=NullPool,
            connect_args={
                "statement_cache_size": 0,
                "prepared_statement_cache_size": 0,
                "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
            },
        )
    return create_async_engine(database_url, echo=echo, pool_pre_ping=True)


def init_engine(
    database_url: str, *, transaction_pooler: bool = False, echo: bool = False
) -> AsyncEngine:
    global _engine, _session_factory
    _engine = make_engine(database_url, transaction_pooler=transaction_pooler, echo=echo)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database engine is not initialised; call init_engine() at startup.")
    return _session_factory


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine is not initialised; call init_engine() at startup.")
    return _engine


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields one session per request."""
    if _session_factory is None:
        raise RuntimeError("Database engine is not initialised; call init_engine() at startup.")
    async with _session_factory() as session:
        yield session


async def check_database() -> None:
    """Raise if Postgres cannot answer a trivial query."""
    async with get_engine().connect() as connection:
        await connection.execute(text("SELECT 1"))
