"""Load synthetic shop data into PostgreSQL.

Usage (from backend/):  python -m scripts.seed

Implemented in Phase 1: shops, products, stock movements, sales, suppliers,
purchase orders and a customer credit ledger, generated with a fixed random seed
so every run produces the same data.
"""

import sys


def main() -> int:
    print("Seeding is implemented in Phase 1.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
