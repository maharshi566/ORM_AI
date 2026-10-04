"""Shared Redis client: session memory (Phase 4), caching and rate limits (Phase 6)."""

from redis.asyncio import Redis

_client: Redis | None = None


def init_redis(redis_url: str) -> Redis:
    global _client
    _client = Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    return _client


def get_redis() -> Redis:
    if _client is None:
        raise RuntimeError("Redis client is not initialised; call init_redis() at startup.")
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def check_redis() -> None:
    """Raise if Redis does not answer PING."""
    await get_redis().ping()
