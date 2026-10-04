"""Dependency health checks behind GET /api/health."""

import asyncio
import time
from collections.abc import Awaitable, Callable

from app.config.settings import Settings
from app.core.logging import get_logger
from app.models.database import check_database
from app.models.schemas import DependencyStatus, HealthResponse
from app.services.redis_client import check_redis

logger = get_logger(__name__)

Check = Callable[[], Awaitable[None]]


async def run_check(name: str, check: Check, timeout_seconds: float) -> DependencyStatus:
    start = time.perf_counter()
    try:
        await asyncio.wait_for(check(), timeout=timeout_seconds)
    except Exception as exc:  # any failure means "not healthy"
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.warning("health_check_failed", dependency=name, error=repr(exc))
        # Only the exception type goes to the client: no hosts, ports or credentials.
        return DependencyStatus(status="error", latency_ms=latency_ms, error=type(exc).__name__)
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    return DependencyStatus(status="ok", latency_ms=latency_ms)


async def collect_health(settings: Settings) -> HealthResponse:
    timeout = settings.health_check_timeout_seconds
    database, redis = await asyncio.gather(
        run_check("database", check_database, timeout),
        run_check("redis", check_redis, timeout),
    )
    checks = {"database": database, "redis": redis}
    overall = "ok" if all(c.status == "ok" for c in checks.values()) else "degraded"
    return HealthResponse(
        status=overall,
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        checks=checks,
    )
