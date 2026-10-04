"""Give ChromaDB room to keep its search indexes in memory.

The problem
-----------
ChromaDB keeps each collection's search index in a small in-memory cache. The size of
that cache is not a setting. ChromaDB works it out from one number: how many files the
operating system lets the program keep open, divided by five. The cache is also split
into 64 parts, and two indexes that land in the same part push each other out.

* A Linux server allows thousands of open files, so the cache is huge and nothing is
  ever pushed out.
* Windows reports 512 and macOS usually 256. The cache then has about 100 or 50
  slots, and with our five collections roughly one run in ten loses an index.

An index that is pushed out before it was saved to disk is gone (ChromaDB only saves
after 1,000 changes), and the next search fails with::

    Error executing plan: Internal error: Error creating hnsw segment reader:
    Nothing found on disk

Searches worked on one run and failed on the next, which made this hard to track down.

The fix
-------
Ask the operating system for a higher open-file limit before ChromaDB starts.
``raise_open_file_limit()`` does that. ``VectorStore`` calls it just before it creates
the ChromaDB client, because ChromaDB reads the limit once, when the client is created.

Nothing here is dangerous: the limit is only a ceiling. It lets the program open more
files if it ever needs to; it does not open any.
"""

import sys
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# At or above this ChromaDB's cache has 6+ slots per part, so in practice nothing is pushed out.
ENOUGH = 2048

# Highest first. macOS refuses anything above 10,240 and Windows' C library stops at 2,048
# or 8,192 depending on its version, so we take the first value the system accepts.
_POSIX_TRIES = (65536, 16384, 10240, 8192, 4096, ENOUGH, 1024)
_WINDOWS_TRIES = (8192, 4096, ENOUGH, 1024)


def raise_open_file_limit() -> int | None:
    """Raise the open-file limit as far as the system allows. Never raises, never lowers.

    Returns the limit in force afterwards, or ``None`` if it could not be read.
    """
    try:
        if sys.platform == "win32":
            import ctypes

            before, after = _raise_windows(ctypes.cdll.msvcrt)
        else:
            import resource  # does not exist on Windows

            before, after = _raise_posix(resource)
    except Exception as exc:  # a limit we could not raise must never stop the app
        logger.warning("open_file_limit_unchanged", reason=f"{type(exc).__name__}: {exc}")
        return None
    if after is not None and before is not None and after > before:
        logger.info("open_file_limit_raised", before=before, after=after)
    return after


def _raise_posix(resource: Any) -> tuple[int | None, int | None]:
    """``resource`` is Python's own module of that name (passed in so tests can fake it)."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    unlimited = resource.RLIM_INFINITY
    if soft == unlimited or soft >= ENOUGH:
        return soft, soft
    for target in _POSIX_TRIES:
        if target <= soft:
            break
        if hard != unlimited and target > hard:
            continue
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (ValueError, OSError):
            continue
        return soft, target
    if hard != unlimited and hard > soft:  # the system allows less than every value above
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
        except (ValueError, OSError):
            return soft, soft
        return soft, hard
    return soft, soft


def _raise_windows(crt: Any) -> tuple[int | None, int | None]:
    """``crt`` is msvcrt.dll, the same library ChromaDB asks for the limit."""
    current = int(crt._getmaxstdio())
    if current >= ENOUGH:
        return current, current
    for target in _WINDOWS_TRIES:
        if target <= current:
            break
        if int(crt._setmaxstdio(target)) == target:  # -1 means "not allowed"
            return current, target
    return current, int(crt._getmaxstdio())
