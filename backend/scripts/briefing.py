"""Print a shop's morning briefing, or run the morning briefings for every shop.

Run from the backend/ folder:

    python -m scripts.briefing --shop SHOP-001          # print today's briefing
    python -m scripts.briefing --all                    # build every shop's (and send,
                                                        # with MESSAGING_PROVIDER=whatsapp)

On a server, cron can run ``--all`` each morning instead of the GitHub Actions workflow
(.github/workflows/briefing.yml), for example at 7:30 IST:

    30 7 * * *  cd /srv/orm_ai && docker compose -f docker-compose.prod.yml \
                  exec -T backend python -m scripts.briefing --all
"""

import argparse
import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config.settings import get_settings
from app.models.database import make_engine
from app.services.briefing_service import run_briefings, todays_briefing
from app.tools.api_tools import default_clients
from app.tools.base import business_now


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ORM_AI morning briefings.")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--shop", help="print this shop's briefing, e.g. SHOP-001")
    which.add_argument("--all", action="store_true", help="build (and send) every shop's")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = make_engine(settings.database_url, transaction_pooler=settings.db_transaction_pooler)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = business_now(settings.business_date)
    try:
        if args.shop:
            row = await todays_briefing(factory, args.shop.upper(), now)
            print(row.text)
            return 0
        messaging = (
            default_clients(settings)["messaging_api"]
            if settings.messaging_provider == "whatsapp"
            else None
        )
        result = await run_briefings(factory, settings, messaging, now)
    finally:
        await engine.dispose()
    print(
        f"Built {result.built} briefings; sent {result.sent} on WhatsApp; "
        f"{result.not_sent} without a real owner number."
    )
    for problem in result.failed:
        print(f"  failed: {problem}")
    return 1 if result.failed else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
