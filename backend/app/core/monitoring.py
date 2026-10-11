"""Monitoring: error reports to Sentry, and metrics for Prometheus (docs/monitoring.md).

* **Sentry** (``SENTRY_DSN``): every unhandled error, with its stack trace and the
  request it happened in, arrives as an alert. Nothing personal is sent: no request
  bodies, cookies or login headers, and phone numbers and emails in messages are masked
  (the same masking as the logs). Expected answers such as 401, 404 or 429 are not
  errors and are not sent.
* **Metrics** (``GET /metrics``, Prometheus text format): requests and their timing by
  route, agent workflows by outcome, agent steps, model tokens by model, tool calls,
  WhatsApp messages, logins and approval decisions. Grafana Cloud's free tier (or any
  Prometheus) can scrape it. With ``METRICS_TOKEN`` set it needs
  ``Authorization: Bearer <token>``; in production without a token it is switched off.

Labels are kept to small, known sets (route templates, not raw paths; tool names;
outcomes), so the number of series stays bounded.
"""

from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from app.config.settings import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter(
    "orm_http_requests_total", "HTTP requests answered", ["method", "route", "status"],
    registry=REGISTRY,
)  # fmt: skip
HTTP_SECONDS = Histogram(
    "orm_http_request_seconds",
    "Time to answer an HTTP request (a streamed chat counts until its stream ends)",
    ["method", "route"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 80),
    registry=REGISTRY,
)
WORKFLOWS = Counter(
    "orm_workflows_total", "Agent workflows finished, by outcome", ["outcome"], registry=REGISTRY
)
AGENT_STEPS = Counter(
    "orm_agent_steps_total", "Agent steps run", ["agent", "status"], registry=REGISTRY
)
LLM_TOKENS = Counter(
    "orm_llm_tokens_total", "Model tokens used", ["model", "direction"], registry=REGISTRY
)
TOOL_CALLS = Counter("orm_tool_calls_total", "Tool calls", ["tool", "status"], registry=REGISTRY)
MESSAGES = Counter(
    "orm_messages_total", "WhatsApp messages sent or refused", ["purpose", "result"],
    registry=REGISTRY,
)  # fmt: skip
LOGINS = Counter("orm_logins_total", "Login attempts", ["method", "result"], registry=REGISTRY)
APPROVAL_DECISIONS = Counter(
    "orm_approval_decisions_total", "Approval decisions", ["decision"], registry=REGISTRY
)
CLIENT_ERRORS = Counter("orm_client_errors_total", "Errors the website reported", registry=REGISTRY)
BUILD = Gauge(
    "orm_build_info", "Always 1; the labels say which build runs", ["version", "env"],
    registry=REGISTRY,
)  # fmt: skip

# Headers that must never leave the server.
_SECRET_HEADERS = {"authorization", "cookie", "x-cron-secret", "x-hub-signature-256"}


def _scrub(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Sentry's before_send: drop expected errors, and anything personal or secret."""
    from app.agents.guardrails import mask_pii
    from app.core.exceptions import AppError

    exc_info = hint.get("exc_info")
    if exc_info and isinstance(exc_info[1], AppError) and exc_info[1].status_code < 500:
        return None  # a 4xx is an answer, not an error
    request = event.get("request") or {}
    request.pop("data", None)  # request bodies: messages about customers and money
    request.pop("cookies", None)
    headers = request.get("headers")
    if isinstance(headers, dict):
        request["headers"] = {k: v for k, v in headers.items() if k.lower() not in _SECRET_HEADERS}
    if "query_string" in request:
        request["query_string"] = mask_pii(str(request["query_string"]))
    for value in (event.get("exception") or {}).get("values") or []:
        if isinstance(value.get("value"), str):
            value["value"] = mask_pii(value["value"])
    if isinstance((event.get("logentry") or {}).get("message"), str):
        event["logentry"]["message"] = mask_pii(event["logentry"]["message"])
    return event


def init_monitoring(settings: Settings) -> bool:
    """Record the build in the metrics and start Sentry when SENTRY_DSN is set.
    Returns True when Sentry is on."""
    BUILD.labels(version=settings.app_version, env=settings.app_env).set(1)
    dsn = settings.sentry_dsn.get_secret_value().strip() if settings.sentry_dsn else ""
    if not dsn:
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=settings.app_env,
        release=f"orm-ai@{settings.app_version}",
        send_default_pii=False,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        before_send=_scrub,  # type: ignore[arg-type]
        max_request_body_size="never",
    )
    logger.info("sentry_enabled", environment=settings.app_env)
    return True


def report_client_error(message: str) -> None:
    """Send an error the website reported to Sentry, when it is on."""
    CLIENT_ERRORS.inc()
    try:
        import sentry_sdk

        if sentry_sdk.get_client().is_active():
            sentry_sdk.capture_message(message, level="error")
    except Exception:  # monitoring must never break the request
        logger.warning("client_error_not_forwarded")
