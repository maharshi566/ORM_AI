"""Rate limits: how often one user (or one address) may call the expensive endpoints.

Each model-backed call costs money or free-tier quota, so chat, agent runs and
approvals share one limit (``RATE_LIMIT_AGENT``, default 20 a minute) and uploads have
their own (``RATE_LIMIT_UPLOAD``). The key is the logged-in user, or the client's
address when there is no token. Over the limit, the API answers HTTP 429 with a
``Retry-After`` header and a plain sentence.

It uses ``limits``, the library behind SlowAPI, directly as a FastAPI dependency, so
each app has its own counters (tests stay independent) and no decorators are needed.
The counters live in memory (one backend process) or, with ``RATE_LIMIT_STORAGE``
set to a Redis URL, in Redis, shared by every process.
"""

import math
import time
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from limits import parse
from limits.aio.storage import MemoryStorage, Storage
from limits.aio.strategies import MovingWindowRateLimiter
from limits.storage import storage_from_string

from app.config.settings import Settings, get_settings
from app.core.auth import Principal, current_user
from app.core.exceptions import AppError


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"


def _storage(settings: Settings) -> Storage:
    uri = settings.rate_limit_storage.strip()
    if not uri or uri == "memory":
        return MemoryStorage()
    if uri.startswith("redis://") or uri.startswith("rediss://"):
        uri = f"async+{uri}"
    return storage_from_string(uri)  # type: ignore[return-value]


def limiter_for(request: Request) -> MovingWindowRateLimiter:
    """The app's limiter, made on first use (one per app, so tests stay independent)."""
    state = request.app.state
    limiter = getattr(state, "rate_limiter", None)
    if limiter is None:
        limiter = MovingWindowRateLimiter(_storage(state.settings))
        state.rate_limiter = limiter
    return limiter


def _address(request: Request) -> str:
    return request.client.host if request.client else "-"


async def _count(request: Request, settings: Settings, kind: str, who: str) -> None:
    if not settings.rate_limit_enabled:
        return
    rule = parse(settings.rate_limit_upload if kind == "upload" else settings.rate_limit_agent)
    limiter = limiter_for(request)
    if await limiter.hit(rule, kind, who):
        return
    stats = await limiter.get_window_stats(rule, kind, who)
    wait = max(1, math.ceil(stats.reset_time - time.time()))
    per = (
        rule.GRANULARITY.name
        if rule.multiples == 1
        else f"{rule.multiples} {rule.GRANULARITY.name}s"
    )
    raise RateLimitedError(
        f"Too many requests: the limit is {rule.amount} per {per}. Try again in {wait} seconds.",
        details={"retry_after_seconds": wait},
    )


def rate_limit(kind: str) -> Callable[..., Awaitable[None]]:
    """A dependency that counts one call of ``kind`` for the logged-in user (or the
    address, without a token): "agent" (chat, agent runs, approvals) or "upload"
    (uploads and ingestion)."""

    async def check(
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        principal: Annotated[Principal | None, Depends(current_user)],
    ) -> None:
        await _count(request, settings, kind, principal.user_id if principal else _address(request))

    return check


async def login_rate_limit(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> None:
    """Logins are counted by address (the caller has no token yet), with the agent limit."""
    await _count(request, settings, "login", _address(request))
