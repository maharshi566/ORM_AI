"""ORM_AI backend entry point.

Run locally with:  uvicorn app.main:app --reload
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.config.settings import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import (
    REQUEST_ID_HEADER,
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
)
from app.core.tracing import configure_tracing
from app.models.database import dispose_engine, init_engine
from app.services.redis_client import close_redis, init_redis

logger = get_logger(__name__)


STREAM_SHUTDOWN_SECONDS = 30


async def _finish_streams(app: FastAPI) -> None:
    """Let streamed chats that are still running finish before the database closes.

    A workflow stopped halfway could leave an approved action unreported. Whatever is
    still running after STREAM_SHUTDOWN_SECONDS is cancelled; its workflow then shows
    as failed or stalled, and an approval can be sent again.
    """
    tasks = [task for task in app.state.stream_tasks if not task.done()]
    if not tasks:
        return
    logger.info("waiting_for_streams", count=len(tasks))
    _, pending = await asyncio.wait(tasks, timeout=STREAM_SHUTDOWN_SECONDS)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        logger.warning("streams_cancelled_at_shutdown", count=len(pending))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    if settings.is_production and (problems := settings.production_problems()):
        for problem in problems:
            logger.error("unsafe_production_settings", problem=problem)
        raise RuntimeError("Refusing to start in production: " + " ".join(problems))
    configure_tracing(settings)
    from app.services.documents_service import sweep_staging

    sweep_staging(settings)
    init_engine(settings.database_url, transaction_pooler=settings.db_transaction_pooler)
    init_redis(settings.redis_url)
    logger.info("app_started", environment=settings.app_env, version=settings.app_version)
    try:
        yield
    finally:
        await _finish_streams(app)
        await close_redis()
        await dispose_engine()
        logger.info("app_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Multi-agent AI assistant that keeps local shops' records organised.",
        lifespan=lifespan,
        openapi_url=f"{settings.api_prefix}/openapi.json",
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.knowledge_retriever = None  # built on the first search (see api/routes/knowledge.py)
    app.state.knowledge_lock = asyncio.Lock()
    app.state.agent_runtime = None  # built on the first chat (see services/chat_service.py)
    app.state.agent_lock = asyncio.Lock()
    app.state.stream_tasks = set()  # streamed chats still running (api/routes/chat.py)
    app.dependency_overrides[get_settings] = lambda: settings

    # The last middleware added runs first: request IDs wrap everything, then CORS (so
    # that even a "too large" answer reaches the browser), then the body size limit.
    app.add_middleware(
        BodySizeLimitMiddleware,
        upload_max_bytes=int(settings.upload_max_mb * 1_000_000),
        upload_path=f"{settings.api_prefix}/documents/upload",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER],
    )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "docs": "/docs",
            "health": f"{settings.api_prefix}/health",
        }

    return app


app = create_app()
