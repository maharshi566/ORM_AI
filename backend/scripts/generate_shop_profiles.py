"""Write the generated shop profiles (SHOP-006 to SHOP-050) into the knowledge base.

Usage (from backend/, with the virtual environment active):

    python -m scripts.generate_shop_profiles           # write or refresh the files
    python -m scripts.generate_shop_profiles --check   # only compare; exit 1 if out of date

The profiles are built from the shop catalogue (app/seed/catalog.py). Run this after
changing a shop there, then run `python -m scripts.ingest` so search sees the change.
SHOP-001 to SHOP-005 have hand-written profiles that this script never touches.
"""

import argparse
import sys

from app.config.paths import backend_path
from app.config.settings import get_settings
from app.seed.profiles import generated_profiles, out_of_date, write_profiles


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="compare only; write nothing")
    parser.add_argument("--kb-dir", help="knowledge-base folder (default KNOWLEDGE_BASE_DIR)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    kb_dir = backend_path(args.kb_dir or get_settings().knowledge_base_dir)
    if args.check:
        problems = out_of_date(kb_dir)
        if problems:
            print("Generated shop profiles need refreshing:", file=sys.stderr)
            print(*problems, sep="\n  ", file=sys.stderr)
            print("Run: python -m scripts.generate_shop_profiles", file=sys.stderr)
            return 1
        print(f"All {len(generated_profiles())} generated shop profiles are up to date.")
        return 0
    written = write_profiles(kb_dir)
    print(f"Wrote {len(written)} shop profiles to {kb_dir / 'shops'}.")
    print("Next: python -m scripts.ingest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
