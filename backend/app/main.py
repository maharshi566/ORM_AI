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
from app.core.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from app.core.tracing import configure_tracing
from app.models.database import dispose_engine, init_engine
from app.services.redis_client import close_redis, init_redis

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_tracing(settings)
    init_engine(settings.database_url, transaction_pooler=settings.db_transaction_pooler)
    init_redis(settings.redis_url)
    logger.info("app_started", environment=settings.app_env, version=settings.app_version)
    try:
        yield
    finally:
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
    app.dependency_overrides[get_settings] = lambda: settings

    # The last middleware added runs first, so request IDs wrap everything else.
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
