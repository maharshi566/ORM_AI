"""Structured logging with structlog.

Every log line carries the context bound for the current request (``request_id``
now; ``workflow_id``, ``session_id`` and ``agent`` from Phase 4). Set
``LOG_JSON=true`` for one JSON object per line, which is what you want in Docker
and in production.

Two processors run on every line before it is written: credentials (keys, passwords,
tokens) are replaced by ``***``, and personal data is masked (Phase 5): values of keys
such as ``phone``, ``email`` or ``address`` become ``***``, and phone numbers or email
addresses inside any other text become ``[phone]`` and ``[email]``.
"""

import logging
import sys
from typing import Any

import structlog

# Keys whose values must never reach a log line.
SENSITIVE_KEY_PARTS = ("password", "secret", "token", "api_key", "apikey", "authorization")
# Token *counts* from model calls contain the word "token" but are not credentials.
TOKEN_COUNT_KEYS = frozenset(
    {"input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens"}
)
MASK = "***"


def mask_sensitive_fields(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Replace the value of any key that looks like a credential."""
    for key in list(event_dict):
        lowered = key.lower()
        if lowered in TOKEN_COUNT_KEYS:
            continue
        if any(part in lowered for part in SENSITIVE_KEY_PARTS):
            event_dict[key] = MASK
    return event_dict


def _mask_value(value: Any, depth: int = 0) -> Any:
    from app.agents.guardrails import is_pii_key, mask_pii

    if isinstance(value, str):
        return mask_pii(value)
    if depth >= 4:
        return value
    if isinstance(value, dict):
        return {
            k: MASK if isinstance(k, str) and is_pii_key(k) else _mask_value(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_mask_value(v, depth + 1) for v in value]
    return value


def mask_personal_data(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Mask phone numbers, email addresses and address fields (POL-DATA-001)."""
    from app.agents.guardrails import is_pii_key

    for key, value in list(event_dict.items()):
        if key in {"timestamp", "level", "logger"}:
            continue
        if is_pii_key(key):
            event_dict[key] = MASK
        else:
            event_dict[key] = _mask_value(value)
    return event_dict


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    """Route both structlog and stdlib logging through one formatter."""
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        mask_sensitive_fields,
        mask_personal_data,
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
