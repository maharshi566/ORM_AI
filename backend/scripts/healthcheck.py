"""Docker's health check: exit 0 when GET /api/health answers 200 on the server's port.

The port follows ``PORT`` (scripts/start.py), so the check works wherever the server
listens.
"""

import os
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("PORT", "8000").strip() or "8000"
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/api/health", timeout=4) as answer:  # noqa: S310 - our own server
            return 0 if answer.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
