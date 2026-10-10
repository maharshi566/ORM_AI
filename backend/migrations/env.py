"""Alembic migration environment for ORM_AI.

The database URL comes from app settings (DATABASE_URL in .env), so migrations
always target the same database as the app. ``target_metadata`` is the full
schema from app.models, which lets ``alembic revision --autogenerate`` compare
the models with the live database and write the difference as a migration.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import CheckConstraint, pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.config.settings import get_settings
from app.models.database import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# An explicit URL (alembic -x url=... or tests) wins over the app settings.
database_url = context.get_x_argument(as_dictionary=True).get("url") or get_settings().database_url
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = Base.metadata


def render_item(type_: str, obj: object, _autogen_context: object) -> str | None | bool:
    """Skip CHECK constraints that belong to an enum column.

    The enum column already creates its own CHECK constraint. With SQLAlchemy 2.1,
    Alembic no longer recognises those constraints as enum-owned and would write
    them into the migration again, which fails with "constraint already exists".
    Returning None skips the item; False means "render it the normal way".
    """
    if type_ == "check" and isinstance(obj, CheckConstraint) and getattr(obj, "_type_bound", False):
        return None
    return False


def _configure(**kwargs: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,  # also detect column type changes
        render_item=render_item,
        render_as_batch=database_url.startswith("sqlite"),  # SQLite needs batch ALTERs
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Write the SQL to stdout instead of running it (alembic upgrade --sql)."""
    _configure(url=database_url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # Transaction-mode poolers (e.g. Supabase port 6543) cannot use prepared statements.
    connect_args = (
        {"statement_cache_size": 0, "prepared_statement_cache_size": 0}
        if get_settings().db_transaction_pooler
        else {}
    )
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def cannot_connect(exc: BaseException) -> str:
    """One plain sentence instead of a 100-line traceback. Never includes the password."""
    url = make_url(database_url)
    where = f"{url.host or 'localhost'}:{url.port or 5432}"
    if url.host in (None, "localhost", "127.0.0.1"):
        fix = (
            "Is Docker Desktop running? Start the database from the project folder with "
            "'docker compose up -d postgres redis', wait until 'docker compose ps' says "
            "healthy, then run this again."
        )
    else:
        fix = "Check the address in DATABASE_URL in .env and your internet connection."
    return f"Cannot reach PostgreSQL at {where} ({type(exc).__name__}). {fix}"


if context.is_offline_mode():
    run_migrations_offline()
else:
    try:
        asyncio.run(run_async_migrations())
    except (OSError, TimeoutError) as exc:  # refused, unknown host, network down
        raise SystemExit(cannot_connect(exc)) from None
