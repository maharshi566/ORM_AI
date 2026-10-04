"""Embed knowledge_base/ documents into ChromaDB.

Usage (from backend/):  python -m scripts.ingest

Implemented in Phase 3. This is the only place embeddings are created: the API
never re-embeds on startup, and unchanged chunks are skipped by content hash.
"""

import sys


def main() -> int:
    print("Ingestion is implemented in Phase 3.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
