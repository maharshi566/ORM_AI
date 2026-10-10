"""Integration test: run the real Alembic migrations against PostgreSQL.

The full migration test is skipped unless TEST_DATABASE_URL points at a disposable
database (the "cannot connect" test needs no database), for example:

    TEST_DATABASE_URL=postgresql+asyncpg://orm_ai:...@localhost:5432/orm_ai_test pytest
"""

import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
BACKEND_DIR = Path(__file__).resolve().parent.parent

needs_postgres = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")


def _alembic(url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.cmd_opts = type("Opts", (), {"x": [f"url={url}"]})()  # same as `alembic -x url=...`
    return config


async def _table_count(url: str) -> int:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        count = await conn.scalar(
            text("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'")
        )
    await engine.dispose()
    return int(count or 0)


async def _tables_without_rls(url: str) -> list[str]:
    """Supabase exposes public tables through its Data API; every table needs RLS."""
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity"
            )
        )
        names = [row[0] for row in rows]
    await engine.dispose()
    return names


def test_an_unreachable_database_gets_one_plain_sentence() -> None:
    """No PostgreSQL listening: a short hint instead of a long traceback."""
    with pytest.raises(SystemExit) as stopped:
        command.upgrade(_alembic("postgresql+asyncpg://orm_ai:secret@127.0.0.1:1/orm_ai"), "head")

    message = str(stopped.value)
    assert message.startswith("Cannot reach PostgreSQL at 127.0.0.1:1")
    assert "docker compose up -d postgres redis" in message
    assert "secret" not in message


@needs_postgres
def test_upgrade_check_and_downgrade() -> None:
    # Synchronous on purpose: Alembic's env.py starts its own event loop.
    assert TEST_DATABASE_URL
    config = _alembic(TEST_DATABASE_URL)

    command.downgrade(config, "base")
    command.upgrade(config, "head")
    # every model's table, plus alembic_version
    assert asyncio.run(_table_count(TEST_DATABASE_URL)) == len(Base.metadata.tables) + 1
    command.check(config)  # raises if the models and the migrations disagree
    assert asyncio.run(_tables_without_rls(TEST_DATABASE_URL)) == []

    command.downgrade(config, "base")
    assert asyncio.run(_table_count(TEST_DATABASE_URL)) == 1
    command.upgrade(config, "head")
