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
import os
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
GEMINI_HOST = "generativelanguage.googleapis.com"
GEMINI_URL = f"https://{GEMINI_HOST}/v1beta/openai"
GEMINI_KEYS_PAGE = "aistudio.google.com/apikey"
# Never copy something that looks like a key into a message or log line.
KEY_PATTERN = re.compile(r"\b(?:(?:sk|lsv2)[-_][A-Za-z0-9_*-]{4,}|AIza[0-9A-Za-z_-]{12,})")


class LLMConfigError(ValueError):
    """The LLM settings cannot be used, for example a base URL without ``http://``."""


@dataclass(frozen=True, slots=True, repr=False)
class LLMEndpoint:
    """One place that model calls go to."""

    purpose: Purpose
    base_url: str | None  # None means OpenAI itself
    api_key: str  # sent as the Bearer token; see __repr__
    key_source: str = ""  # the setting the key came from; "" means no key is set
    # True when that setting is a real environment variable, which wins over .env
    # (on Windows: one set in System Properties or with setx, often long forgotten).
    key_from_environment: bool = False

    def __repr__(self) -> str:
        # Written by hand so that neither the key nor a password inside the URL can end
        # up in a log line or a traceback.
        key = f"from {self.key_source}" if self.has_key else "none"
        return f"LLMEndpoint({self.purpose}, {self.where}, key {key})"

    @property
    def is_gateway(self) -> bool:
        return self.base_url is not None

    @property
    def is_gemini(self) -> bool:
        """True for Google's OpenAI-style endpoint (see GEMINI_URL)."""
        return bool(self.base_url) and urlsplit(self.base_url or "").hostname == GEMINI_HOST

    @property
    def has_key(self) -> bool:
        return bool(self.key_source)

    @property
    def key_hint(self) -> str:
        """``sk-proj-…a1b2``: enough to find the key in the provider's list, never more."""
        if not self.has_key:
            return "none"
        key = self.api_key
        prefix = next(
            (p for p in ("sk-proj-", "sk-svcacct-", "sk-or-", "sk-", "AIza") if key.startswith(p)),
            "",
        )
        tail = key[-4:] if len(key) >= 16 else ""
        return f"{prefix}…{tail}"

    @property
    def key_origin(self) -> str:
        """Where the key was read from, for messages: '.env' or the environment variable."""
        if not self.has_key:
            return ""
        if self.key_from_environment:
            return f"the {self.key_source} environment variable, which wins over .env"
        return f"{self.key_source} in .env"

    @property
    def where(self) -> str:
        """For messages: ``OpenAI`` or ``the gateway at http://localhost:20128/v1``."""
        if self.base_url is None:
            return "OpenAI"
        if self.is_gemini:
            return "Google Gemini"
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


def _from_environment(setting: str) -> bool:
    """True when ``setting`` is set as a real environment variable (any case, as Windows)."""
    return bool(setting) and any(name.upper() == setting for name in os.environ)


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

    from_env = _from_environment(source)
    if url is None:
        return LLMEndpoint(purpose, None, key, source, from_env) if key else None
    return LLMEndpoint(purpose, url, key or PLACEHOLDER_KEY, source, from_env)


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


def _error_body(exc: openai.APIStatusError) -> dict[str, object]:
    """The JSON error object. OpenAI sends one object; Google sends a list of them."""
    body = exc.body
    if isinstance(body, list) and body and isinstance(body[0], dict):
        body = body[0].get("error", body[0])
    return body if isinstance(body, dict) else {}


def _server_detail(exc: openai.APIStatusError) -> str:
    """The server's own error sentence, shortened; empty when it did not send one.

    Only the ``message`` field of a JSON error is used. A web page (for example a proxy's
    "502 Bad Gateway") or a raw body is never copied into our messages.
    """
    text = _error_body(exc).get("message")
    if not isinstance(text, str):
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    text = KEY_PATTERN.sub("[key]", text)  # never echo a key back
    return f" The server said: {text[:240]}" if text else ""


def _error_code(exc: openai.APIStatusError) -> str:
    """The provider's machine-readable error code, such as 'insufficient_quota'."""
    code = getattr(exc, "code", None)
    body = _error_body(exc)
    if not code:
        code = body.get("code") or body.get("type")
    return str(code or "")


def is_key_rejected(exc: Exception) -> bool:
    """True when the provider refused the key itself.

    OpenAI and most gateways answer 401. Google answers 400 with API_KEY_INVALID
    ("API key not valid"), so a plain status check would call it a bad request.
    """
    if isinstance(exc, openai.AuthenticationError):
        return True
    if not isinstance(exc, openai.BadRequestError):
        return False
    text = str(exc.body).lower()
    return any(
        marker in text for marker in ("api_key_invalid", "api key not valid", "api key expired")
    )


def explain_error(
    exc: Exception, endpoint: LLMEndpoint, *, doing: str, model: str = ""
) -> LLMFailure:
    """Describe an SDK error in plain words: what failed, where, and what to try.

    ``doing`` is the activity ("embeddings", "chat"). Provider error text is added only
    for errors that cannot carry a key; a rejected key gets a fixed sentence instead.
    """
    where = endpoint.where
    gateway = endpoint.is_gateway and not endpoint.is_gemini

    if is_key_rejected(exc):
        if endpoint.has_key and endpoint.key_from_environment:
            fix = (
                f"The key comes from the {endpoint.key_source} environment variable on this "
                "computer, which wins over .env. Remove that variable, or put a valid key in "
                'it (docs/agents.md, "The key is rejected").'
            )
        elif endpoint.has_key and endpoint.is_gemini:
            fix = (
                f"Check {endpoint.key_source} in .env: compare its last four characters with "
                f"your keys at {GEMINI_KEYS_PAGE}. A key cut short when pasting, or one that "
                "was deleted, gives this error. Create a new key there if unsure."
            )
        elif endpoint.is_gemini:
            fix = f"No key is set. Create one at {GEMINI_KEYS_PAGE} and put it in LLM_API_KEY."
        elif endpoint.has_key and gateway:
            fix = (
                f"Check {endpoint.key_source} in .env and the key shown in the gateway's dashboard."
            )
        elif endpoint.has_key:
            fix = (
                f"Check {endpoint.key_source} in .env: compare its last four characters with "
                "your keys at platform.openai.com/api-keys. A deleted or revoked key, or one "
                "cut short when pasting, gives this error; create a new key if unsure."
            )
        elif gateway:
            fix = (
                "No key is set. Copy one from the gateway's dashboard into LLM_API_KEY "
                "(or EMBEDDING_API_KEY) in .env."
            )
        else:
            fix = "Check OPENAI_API_KEY in .env."
        return LLMFailure(f"{where} rejected the API key. {fix}", retryable=False)

    if isinstance(exc, openai.PermissionDeniedError):
        hint = (
            f" Create the key in Google AI Studio ({GEMINI_KEYS_PAGE}). A key from another "
            "Google Cloud project needs the Generative Language API turned on. The Gemini API "
            "is also not offered in every country."
            if endpoint.is_gemini
            else " Check the key's permissions in the gateway's dashboard."
            if gateway
            else " A restricted key needs permission for model requests: at "
            "platform.openai.com/api-keys, edit the key and set Permissions to All (or allow "
            "Model capabilities), and check the project may use this model."
        )
        return LLMFailure(
            f"{where} accepted the API key but refused this request (HTTP 403)."
            f"{_server_detail(exc)}{hint}",
            retryable=False,
        )

    if isinstance(exc, openai.NotFoundError):
        what = f"the model {model!r}" if model else "the model"
        hint = (
            f" Check the spelling of {what}: python -m scripts.check_llm --list shows the "
            "Gemini models your key can use."
            if endpoint.is_gemini
            else f" Check that the URL ends in /v1 and that {what} exists on the gateway "
            "(python -m scripts.check_llm lists its models)."
            if gateway
            else f" Check that {what} exists and your account can use it."
        )
        return LLMFailure(
            f"{where}: {doing} failed with HTTP 404 (not found).{hint}", retryable=False
        )

    if isinstance(exc, openai.RateLimitError) and _error_code(exc) == "insufficient_quota":
        fix = (
            " The provider behind the gateway has no credit left; see the gateway's dashboard."
            if gateway
            else " The key works, but OpenAI's API is prepaid and this account has no credit "
            "left. Add credit at platform.openai.com (Settings, then Billing), set a monthly "
            "limit there, wait a few minutes and try again."
        )
        return LLMFailure(f"{where}: no API credit (insufficient_quota).{fix}", retryable=False)

    if isinstance(exc, openai.RateLimitError) and endpoint.is_gemini:
        return LLMFailure(
            f"Google Gemini: free-tier limit reached{f' for {model}' if model else ''}."
            f"{_server_detail(exc)} Limits are per model, per minute and per day (the day "
            "resets at midnight Pacific time); aistudio.google.com shows yours. Wait a "
            "minute, use a lighter model, or set LLM_MODEL_FALLBACK to another Gemini model.",
            retryable=True,
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
