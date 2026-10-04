"""Load the synthetic shop data into the database.

Run from the backend/ folder, after `alembic upgrade head`:

    python -m scripts.seed                 # first load into an empty database
    python -m scripts.seed --reset         # wipe ALL tables and load again
    python -m scripts.seed --dry-run       # generate and check, write nothing
    python -m scripts.seed --export-dir data/seed/csv   # also write CSV files

The data is deterministic: the same --seed and --anchor always give the same rows,
so a reset always gets you back to exactly the same starting point.
"""

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

from app.config.settings import get_settings
from app.models.database import make_engine
from app.seed.generator import (
    DEFAULT_ANCHOR,
    DEFAULT_DAYS,
    DEFAULT_SEED,
    Dataset,
    generate,
    validate,
)
from app.seed.loader import (
    database_has_shop_data,
    edge_cases_markdown,
    export_csv,
    load_dataset,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load ORM_AI synthetic shop data.")
    parser.add_argument("--reset", action="store_true", help="delete ALL existing rows first")
    parser.add_argument("--dry-run", action="store_true", help="generate and validate only")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--anchor",
        type=date.fromisoformat,
        default=DEFAULT_ANCHOR,
        help="the synthetic 'today' (YYYY-MM-DD)",
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="days of history")
    parser.add_argument("--export-dir", type=Path, help="also write one CSV per table here")
    parser.add_argument("--database-url", help="override DATABASE_URL")
    return parser.parse_args(argv)


def _write_files(dataset: Dataset, export_dir: Path | None) -> list[Path]:
    """Write EDGE_CASES.md (and the CSV files, if asked). Blocking: run in a thread."""
    (BACKEND_DIR / "data" / "seed").mkdir(parents=True, exist_ok=True)
    # newline="\n" keeps LF line endings on Windows too, so git sees no change.
    (BACKEND_DIR / "data" / "seed" / "EDGE_CASES.md").write_text(
        edge_cases_markdown(dataset), encoding="utf-8", newline="\n"
    )
    return export_csv(dataset, export_dir) if export_dir else []


async def run(args: argparse.Namespace) -> int:
    # Generating the data is CPU work and writing files is disk work: both run in a
    # worker thread so the event loop is never blocked.
    dataset = await asyncio.to_thread(generate, seed=args.seed, anchor=args.anchor, days=args.days)
    problems = await asyncio.to_thread(validate, dataset)
    if problems:
        print("Generated data failed validation:", *problems, sep="\n  ", file=sys.stderr)
        return 1

    print(f"Generated data (seed {args.seed}, today = {args.anchor}):")
    for table, count in dataset.counts().items():
        print(f"  {table:<22} {count:>6}")
    print(f"  {'edge cases':<22} {len(dataset.edge_cases):>6}")
    print(f"Fingerprint: {dataset.fingerprint()}")

    files = await asyncio.to_thread(_write_files, dataset, args.export_dir)
    if args.export_dir:
        print(f"Wrote {len(files)} files to {args.export_dir}")

    if args.dry_run:
        print("Dry run: nothing written to the database.")
        return 0

    settings = get_settings()
    url = args.database_url or settings.database_url
    engine = make_engine(url, transaction_pooler=settings.db_transaction_pooler)
    try:
        if not args.reset and await database_has_shop_data(engine):
            print(
                "The database already has shop data. Run with --reset to wipe ALL tables "
                "(including chats and approvals) and load again.",
                file=sys.stderr,
            )
            return 1
        await load_dataset(engine, dataset, reset=args.reset)
    finally:
        await engine.dispose()
    print("Loaded into the database.")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
