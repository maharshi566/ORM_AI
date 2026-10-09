"""Where language-model calls go, and the client that makes them.

Every model call in the app (embeddings and the agents) uses the OpenAI-style API.
Gateways such as OmniRoute, OpenRouter, LiteLLM and Ollama speak the same API, so
moving between OpenAI and a gateway is configuration, not code:

    nothing set        -> OpenAI itself, using OPENAI_API_KEY
    LLM_BASE_URL set   -> that gateway, using LLM_API_KEY (or OPENAI_API_KEY)

Embeddings follow chat unless EMBEDDING_BASE_URL is set. That matters because a
gateway can be a fine place for chat and still have no embedding model, so the two
may live in different places.

``resolve_endpoint`` turns the settings into one ``LLMEndpoint``, ``make_client``
builds the SDK client for it, and ``explain_error`` turns the SDK's exceptions into
sentences a shopkeeper (or a beginner) can act on. Nothing else in the app reads the
URL and key settings directly, and no key ever appears in a message or a log line.

The agents' client (structured output, tool calls, a fallback model, token
accounting) is built on these pieces in ``chat_model.py``.
"""

import ipaddress
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit, urlunsplit

import openai
from openai import AsyncOpenAI
from pydantic import SecretStr

from app.config.settings import Settings

if TYPE_CHECKING:
    import httpx2

Purpose = Literal["chat", "embeddings"]

# The OpenAI SDK refuses an empty key, but a local gateway may not ask for one.
PLACEHOLDER_KEY = "no-key-needed"
OPENAI_URL = "https://api.openai.com/v1"


class LLMConfigError(ValueError):
    """The LLM settings cannot be used, for example a base URL without ``http://``."""


@dataclass(frozen=True, slots=True, repr=False)
class LLMEndpoint:
    """One place that model calls go to."""

    purpose: Purpose
    base_url: str | None  # None means OpenAI itself
    api_key: str  # sent as the Bearer token; see __repr__
    key_source: str = ""  # the setting the key came from; "" means no key is set

    def __repr__(self) -> str:
        # Written by hand so that neither the key nor a password inside the URL can end
        # up in a log line or a traceback.
        key = f"from {self.key_source}" if self.has_key else "none"
        return f"LLMEndpoint({self.purpose}, {self.where}, key {key})"

    @property
    def is_gateway(self) -> bool:
        return self.base_url is not None

    @property
    def has_key(self) -> bool:
        return bool(self.key_source)

    @property
    def where(self) -> str:
        """For messages: ``OpenAI`` or ``the gateway at http://localhost:20128/v1``."""
        if self.base_url is None:
            return "OpenAI"
        return f"the gateway at {_without_credentials(self.base_url)}"


def _without_credentials(url: str) -> str:
    """The URL with any ``user:password@`` part removed, safe to print."""
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _clean_base_url(raw: str, setting: str) -> str | None:
    url = raw.strip().rstrip("/")
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise LLMConfigError(
            f"{setting} is {_without_credentials(raw.strip())!r}, which is not a web address. "
            "It must start with http:// or https://, for example http://localhost:20128/v1."
        )
    return None if url == OPENAI_URL else url  # spelling out OpenAI's address means OpenAI


def _first_key(candidates: tuple[tuple[str, SecretStr | None], ...]) -> tuple[str, str]:
    """(key, name of the setting it came from) for the first candidate that is set."""
    for name, secret in candidates:
        value = secret.get_secret_value().strip() if secret else ""
        if value:
            return value, name
    return "", ""


def resolve_endpoint(settings: Settings, purpose: Purpose = "chat") -> LLMEndpoint | None:
    """Where ``purpose`` calls go, or None when nothing is configured.

    "Nothing configured" means no gateway URL and no key; OpenAI cannot be called
    without a key. A gateway without a key is allowed, because some local gateways
    need none; if it does need one, the first call says so.
    """
    llm_key = ("LLM_API_KEY", settings.llm_api_key)
    openai_key = ("OPENAI_API_KEY", settings.openai_api_key)
    embedding_key = ("EMBEDDING_API_KEY", settings.embedding_api_key)

    if purpose == "embeddings" and settings.embedding_base_url.strip():
        url = _clean_base_url(settings.embedding_base_url, "EMBEDDING_BASE_URL")
        # A separate embedding service has its own key; the chat gateway's key is not
        # sent there by accident.
        key, source = _first_key((embedding_key, openai_key))
    elif purpose == "embeddings":
        url = _clean_base_url(settings.llm_base_url, "LLM_BASE_URL")
        key, source = _first_key((embedding_key, llm_key, openai_key))
    else:
        url = _clean_base_url(settings.llm_base_url, "LLM_BASE_URL")
        key, source = _first_key((llm_key, openai_key))

    if url is None:
        return LLMEndpoint(purpose, None, key, source) if key else None
    return LLMEndpoint(purpose, url, key or PLACEHOLDER_KEY, source)


def make_client(
    endpoint: LLMEndpoint,
    *,
    timeout: float,
    max_retries: int,
    http_client: "httpx2.AsyncClient | None" = None,
) -> AsyncOpenAI:
    """The SDK client for ``endpoint``. ``http_client`` is for tests (a fake gateway)."""
    return AsyncOpenAI(
        api_key=endpoint.api_key,
        base_url=endpoint.base_url,
        timeout=timeout,
        max_retries=max_retries,
        http_client=http_client,
    )


def is_remote_plain_http(endpoint: LLMEndpoint) -> bool:
    """True when keys and shop data would cross the internet without encryption."""
    if endpoint.base_url is None:
        return False
    parts = urlsplit(endpoint.base_url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "http" or not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A name: "localhost", a Docker service name (no dot) or a private suffix is local.
        return "." in host and not host.endswith((".local", ".internal", ".lan", ".localhost"))
    return not (address.is_private or address.is_loopback or address.is_link_local)


@dataclass(frozen=True, slots=True)
class LLMFailure:
    message: str
    retryable: bool  # True when trying again later could work


def _server_detail(exc: openai.APIStatusError) -> str:
    """The server's own error sentence, shortened; empty when it did not send one.

    Only the ``message`` field of a JSON error is used. A web page (for example a proxy's
    "502 Bad Gateway") or a raw body is never copied into our messages.
    """
    body = exc.body
    text = body.get("message") if isinstance(body, dict) else None
    if not isinstance(text, str):
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return f" The server said: {text[:200]}" if text else ""


def explain_error(
    exc: Exception, endpoint: LLMEndpoint, *, doing: str, model: str = ""
) -> LLMFailure:
    """Describe an SDK error in plain words: what failed, where, and what to try.

    ``doing`` is the activity ("embeddings", "chat"). Provider error text is added only
    for errors that cannot carry a key; a rejected key gets a fixed sentence instead.
    """
    where = endpoint.where
    gateway = endpoint.is_gateway

    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        if endpoint.has_key:
            fix = f"Check {endpoint.key_source} in .env"
            fix += " and the key shown in the gateway's dashboard." if gateway else "."
        elif gateway:
            fix = (
                "No key is set. Copy one from the gateway's dashboard into LLM_API_KEY "
                "(or EMBEDDING_API_KEY) in .env."
            )
        else:
            fix = "Check OPENAI_API_KEY in .env."
        return LLMFailure(f"{where} rejected the API key. {fix}", retryable=False)

    if isinstance(exc, openai.NotFoundError):
        what = f"the model {model!r}" if model else "the model"
        hint = (
            f" Check that the URL ends in /v1 and that {what} exists on the gateway "
            "(python -m scripts.check_llm lists its models)."
            if gateway
            else f" Check that {what} exists and your account can use it."
        )
        return LLMFailure(
            f"{where}: {doing} failed with HTTP 404 (not found).{hint}", retryable=False
        )

    if isinstance(exc, openai.RateLimitError):
        hint = (
            " The provider behind the gateway may be out of free quota; see the gateway's "
            "dashboard."
            if gateway
            else " If it keeps happening, check the account's billing and usage limits."
        )
        return LLMFailure(
            f"{where}: rate limit or quota reached. Wait a minute.{hint}", retryable=True
        )

    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        hint = (
            " Is it running? (the bundled OmniRoute starts with: "
            "docker compose --profile gateway up -d omniroute)"
            if gateway
            else ""
        )
        return LLMFailure(
            f"Could not reach {where} (timeout or network error).{hint}", retryable=True
        )

    if isinstance(exc, openai.APIStatusError):
        return LLMFailure(
            f"{where}: {doing} failed with HTTP {exc.status_code}.{_server_detail(exc)}",
            retryable=exc.status_code >= 500,
        )

    return LLMFailure(f"{where}: {doing} failed ({type(exc).__name__}).", retryable=False)
