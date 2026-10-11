"""Add a user who can log in: the first owner or admin of a new server.

Run from the backend/ folder (on a server: ``docker compose ... exec backend``):

    python -m scripts.create_user --name "Ramesh Naidu" --role owner --shop SHOP-001 \\
        --email ramesh@example.com --phone "+91 98765 43210"
    python -m scripts.create_user --name "Admin" --role admin --email admin@example.com

The password is asked for twice and never echoed (or read from standard input with
``--password-stdin``, for scripts). A user with only ``--phone`` logs in with one-time
codes and needs no password. After this, owners add their staff in the app.
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config.settings import get_settings
from app.core.exceptions import AppError
from app.core.passwords import password_problems
from app.models.database import make_engine
from app.services.auth_service import create_user


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Add an ORM_AI user who can log in.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", choices=["owner", "staff", "admin"], required=True)
    parser.add_argument("--shop", help="the shop ID (owners and staff), e.g. SHOP-001")
    parser.add_argument("--email", help="logs in with this email and a password")
    parser.add_argument("--phone", help="logs in with one-time codes sent here")
    parser.add_argument(
        "--password-stdin", action="store_true", help="read the password from standard input"
    )
    parser.add_argument("--no-password", action="store_true", help="phone codes only")
    parser.add_argument("--database-url", help="override DATABASE_URL")
    return parser.parse_args(argv)


def _ask_password(args: argparse.Namespace) -> str | None:
    if args.no_password or not args.email:
        return None
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    while True:
        first = getpass.getpass("Password (at least 10 characters): ")
        problems = password_problems(first, email=args.email, phone=args.phone)
        if problems:
            print(" ".join(problems))
            continue
        if getpass.getpass("Again: ") == first:
            return first
        print("The two passwords differ; try again.")


async def run(args: argparse.Namespace) -> int:
    password = _ask_password(args)
    settings = get_settings()
    engine = make_engine(
        args.database_url or settings.database_url,
        transaction_pooler=settings.db_transaction_pooler,
    )
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            user = await create_user(
                db,
                None,
                name=args.name,
                role=args.role,
                shop_id=args.shop,
                email=args.email,
                phone=args.phone,
                password=password,
            )
    except AppError as exc:
        print(f"Not added: {exc.message}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    ways = [w for w, on in (("email + password", password), ("phone code", user.phone_e164)) if on]
    print(f"Added {user.id} ({user.name}, {user.role}). Logs in with: {', '.join(ways)}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
