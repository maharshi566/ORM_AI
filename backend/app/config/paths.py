"""Paths inside the backend folder.

Settings such as ``CHROMA_PERSIST_DIR=./data/chroma`` are relative paths. They are
resolved against ``backend/``, not the folder a command happens to run from, so
``python -m scripts.ingest`` and the API always use the same directories.
"""

from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]


def backend_path(value: str | Path) -> Path:
    """Return ``value`` as an absolute path; relative paths start at ``backend/``."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (BACKEND_DIR / path).resolve()
