"""LangSmith tracing: see every workflow as a tree of agents, model calls and tools.

Set LANGSMITH_TRACING=true and LANGSMITH_API_KEY in .env (free account at
https://smith.langchain.com). LangGraph then sends a trace of every run: each node,
with its inputs, outputs and timing, and, through the wrapped OpenAI client, every
model call with its prompt, reply and token counts.

Privacy: a trace contains the shop's records that the agents read. Leave tracing off
for real shops unless the owner agrees, or use a self-hosted LangSmith.

The settings are read from .env by pydantic, which does not put them into the
process environment, so they are copied there for the LangSmith library to find.
Structured application logs (structlog) are written either way.
"""

import os

from app.config.settings import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def configure_tracing(settings: Settings) -> bool:
    """Turn LangSmith tracing on when it is configured. Returns whether it is on."""
    key = settings.langsmith_api_key.get_secret_value() if settings.langsmith_api_key else ""
    if not (settings.langsmith_tracing and key):
        if settings.langsmith_tracing:
            logger.warning("langsmith_tracing_needs_key", hint="Set LANGSMITH_API_KEY in .env")
        return False
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    logger.info("langsmith_tracing_on", project=settings.langsmith_project)
    return True
