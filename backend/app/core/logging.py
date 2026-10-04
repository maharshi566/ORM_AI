"""Structured logging with structlog.

Every log line carries the context bound for the current request (``request_id``
now; ``workflow_id``, ``session_id`` and ``agent`` from Phase 4). Set
``LOG_JSON=true`` for one JSON object per line, which is what you want in Docker
and in production.
"""

import logging
import sys
from typing import Any

import structlog

# Keys whose values must never reach a log line.
SENSITIVE_KEY_PARTS = ("password", "secret", "token", "api_key", "apikey", "authorization")
MASK = "***"


def mask_sensitive_fields(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Replace the value of any key that looks like a credential."""
    for key in list(event_dict):
        if any(part in key.lower() for part in SENSITIVE_KEY_PARTS):
            event_dict[key] = MASK
    return event_dict


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    """Route both structlog and stdlib logging through one formatter."""
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        mask_sensitive_fields,
    ]

    renderer: Any
    if json_logs:
        shared_processors.append(structlog.processors.dict_tracebacks)
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Uvicorn's own handlers would print every line twice; let them propagate to root.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # Our middleware already logs each request with its latency and request ID.
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
