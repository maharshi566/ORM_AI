"""Start the backend: the container's command (backend/Dockerfile).

1. Apply the database migrations (``alembic upgrade head``).
2. With ``SEED_IF_EMPTY=true``: load the synthetic demo shops, but only into an empty
   database (one with shops or users is never touched).
3. With ``INGEST_ON_START=true``: build the knowledge index. Hosts without a persistent
   disk (Render's free tier) lose it on every restart; unchanged chunks are skipped
   where the index survived.
4. Serve on ``PORT`` (default 8000; Render sets its own). Behind a proxy, set
   ``FORWARDED_ALLOW_IPS`` (Render: ``*``) so rate limits see each visitor's address
   instead of the proxy's.

Steps 2 and 3 are off unless asked for, so a server with real shop data starts exactly
as before: migrate, then serve.
"""

import asyncio
import os
import subprocess
import sys


def flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def run(*args: str) -> None:
    subprocess.run([sys.executable, "-m", *args], check=True)  # noqa: S603 - fixed commands


async def database_is_empty() -> bool:
    """No shops and no users: a user made with scripts/create_user.py in an empty
    database would otherwise collide with the demo users' IDs."""
    from sqlalchemy import func, select

    from app.config.settings import get_settings
    from app.models import User
    from app.models.database import make_engine
    from app.seed.loader import database_has_shop_data

    settings = get_settings()
    engine = make_engine(settings.database_url, transaction_pooler=settings.db_transaction_pooler)
    try:
        if await database_has_shop_data(engine):
            return False
        async with engine.connect() as conn:
            return not await conn.scalar(select(func.count()).select_from(User))
    finally:
        await engine.dispose()


def main() -> None:
    run("alembic", "upgrade", "head")
    if flag("SEED_IF_EMPTY"):
        if asyncio.run(database_is_empty()):
            print("start: the database is empty; loading the demo shops", flush=True)
            run("scripts.seed")
        else:
            print("start: the database has shops or users; not seeding", flush=True)
    if flag("INGEST_ON_START"):
        run("scripts.ingest")
    port = os.environ.get("PORT", "8000").strip() or "8000"
    command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", port]  # noqa: S104 - a container listens on every interface
    forwarded = os.environ.get("FORWARDED_ALLOW_IPS", "").strip()
    if forwarded:
        command += ["--proxy-headers", "--forwarded-allow-ips", forwarded]
    sys.stdout.flush()
    os.execv(sys.executable, command)  # noqa: S606 - becomes the server process


if __name__ == "__main__":
    main()
