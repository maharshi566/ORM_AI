"""The client the agents use to talk to a language model.

``llm_service`` decides where calls go (OpenAI, OmniRoute, OpenRouter, ...) and turns
SDK errors into plain sentences. This module adds what the agents need on top:

* **Two tiers.** ``fast`` (LLM_MODEL_FAST) for triage and tool selection, ``smart``
  (LLM_MODEL_SMART) for investigation and the final answer. If only one is set, both
  tiers use it.
* **Structured output.** ``structured()`` returns a validated Pydantic object, never
  free text. It asks for strict JSON-schema output first. A gateway or model that
  refuses that request (any HTTP 400/422) gets plain JSON mode with the schema
  written into the prompt instead (remembered per model). If the reply still does not match the
  schema, the model is shown what was wrong and asked once more; after that the
  call fails with ``LLMError(kind="malformed_output")`` rather than guessing.
* **Tool calling.** ``tool_round()`` runs one step: the model either asks for tool
  calls or answers. The agent runs the tools itself (through the ToolRegistry), so
  the model can never invent a tool result. Tool schemas are sent in a portable
  subset of JSON Schema (app/services/portable_schema.py), and the model's turn is
  sent back exactly as the provider wrote it, so Gemini's thought signatures survive.
* **Retries and a fallback model.** The OpenAI SDK already retries timeouts, 429s
  and 5xx errors with exponential backoff (LLM_MAX_RETRIES times). If the model still
  fails, LLM_MODEL_FALLBACK (when set) is tried once. A rejected key is not retried
  on the fallback: it would fail the same way.
* **Token accounting.** Every reply carries an ``LLMUsage`` (model, tokens, time),
  which the graph records per agent in the ``agent_runs`` table.

Tests replace this class with a scripted fake that has the same two methods
(see tests/fake_llm.py), so the agents run without a network or a key.
"""

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

import openai
from openai import AsyncOpenAI
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel, ValidationError

from app.config.settings import Settings
from app.core.logging import get_logger
from app.services.llm_service import (
    LLMConfigError,
    explain_error,
    is_key_rejected,
    make_client,
    resolve_endpoint,
)
from app.services.portable_schema import portable_tools

if TYPE_CHECKING:
    import httpx2

logger = get_logger(__name__)

Tier = Literal["fast", "smart"]
ErrorKind = Literal["not_configured", "unavailable", "refused", "malformed_output", "rejected"]
T = TypeVar("T", bound=BaseModel)

REPAIR_PROMPT = (
    "Your previous reply did not match the required JSON schema. Problems: {problems}. "
    "Reply again with only the corrected JSON object, nothing else."
)


class LLMError(Exception):
    """A model call that failed. ``message`` is safe to show to a shopkeeper."""

    def __init__(
        self,
        message: str,
        *,
        kind: ErrorKind,
        retryable: bool = False,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.retryable = retryable
        self.cause = cause


@dataclass(frozen=True)
class LLMUsage:
    """What one agent's model calls cost: tokens, time and which model answered."""

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    calls: int = 0
    fallback_used: bool = False

    def __add__(self, other: "LLMUsage") -> "LLMUsage":
        return LLMUsage(
            model=other.model or self.model,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            latency_ms=round(self.latency_ms + other.latency_ms, 1),
            calls=self.calls + other.calls,
            fallback_used=self.fallback_used or other.fallback_used,
        )


@dataclass(frozen=True)
class StructuredReply[M: BaseModel]:
    value: M
    usage: LLMUsage
    mode: Literal["json_schema", "json_object"]


@dataclass(frozen=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: str  # JSON text exactly as the model wrote it; the registry validates it


@dataclass(frozen=True)
class ToolRoundReply:
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    # The provider's own assistant turn, with any extra fields it needs back (Gemini 3
    # puts a "thought signature" on each tool call and refuses the next request
    # without it). None for replies built in code, such as the test fakes.
    raw_message: dict[str, Any] | None = None

    def assistant_message(self) -> dict[str, Any]:
        """The model's turn, to send back with the tool results in the next round."""
        if self.raw_message is not None:
            return json.loads(json.dumps(self.raw_message))  # a copy the caller can change
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


class ChatModel(Protocol):
    """What the agents need from a language model. OpenAIChatModel is the real one."""

    def model_for(self, tier: Tier) -> str: ...

    async def structured(
        self, schema: type[T], *, system: str, messages: Sequence[dict[str, Any]], tier: Tier
    ) -> StructuredReply[T]: ...

    async def tool_round(
        self,
        *,
        system: str,
        messages: Sequence[dict[str, Any]],
        tools: list[dict[str, Any]],
        tier: Tier,
    ) -> ToolRoundReply: ...


def chat_model_problem(settings: Settings) -> str | None:
    """Why the agents cannot run with these settings, or None when they can."""
    try:
        endpoint = resolve_endpoint(settings, "chat")
    except LLMConfigError as exc:
        return str(exc)
    if endpoint is None:
        return (
            "No language model is configured. Put OPENAI_API_KEY in .env, or LLM_BASE_URL "
            "(and LLM_API_KEY) for a gateway such as OmniRoute or OpenRouter."
        )
    if not (settings.llm_model_fast.strip() or settings.llm_model_smart.strip()):
        return (
            "No chat model is chosen. Set LLM_MODEL_FAST and LLM_MODEL_SMART in .env "
            "(python -m scripts.check_llm --list shows the models you can use)."
        )
    return None


def _strip_to_json(text: str) -> str:
    """The JSON object inside a reply, without Markdown fences or chatter around it."""
    text = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    return text


def _problems(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [
            f"{'.'.join(str(p) for p in error['loc']) or 'reply'}: {error['msg']}"
            for error in exc.errors(include_url=False)[:6]
        ]
        return "; ".join(parts)
    return "the reply was not valid JSON"


def _request_rejected(exc: Exception | None) -> bool:
    """True when the server refused the request itself (HTTP 400 or 422).

    In "auto" mode a refused strict-schema request is retried once in JSON mode.
    Providers word this error differently (OpenAI names response_format; Gemini
    names the schema keyword it does not know), so any 400/422 counts. If JSON mode
    is refused too, the request was the problem, not the format, and the error is
    raised as usual.
    """
    if exc is None or is_key_rejected(exc):  # Google answers a bad key with 400 too
        return False
    return isinstance(exc, openai.BadRequestError | openai.UnprocessableEntityError)


# Fields of an assistant message that may be sent back to the provider. Everything
# else in a reply (refusal, annotations, audio, ...) is output-only, and OpenAI
# rejects a request that sends it back.
ASSISTANT_FIELDS = ("role", "content", "tool_calls", "extra_content", "reasoning_details")
TOOL_CALL_FIELDS = ("id", "type", "function", "extra_content")


def _dump(value: Any) -> dict[str, Any]:
    """An SDK object as plain JSON, extra provider fields included."""
    if isinstance(value, dict):
        return value
    try:
        dumped = value.model_dump(exclude_none=True)
    except AttributeError:
        return {}
    return dumped if isinstance(dumped, dict) else {}


def _raw_assistant_message(
    message: dict[str, Any], calls: list[tuple[ToolCallRequest, dict[str, Any]]]
) -> dict[str, Any]:
    """The provider's assistant turn as JSON, keeping fields it needs to see again.

    Gemini 3 returns ``tool_calls[i].extra_content.google.thought_signature`` and
    rejects the next request (HTTP 400) if the signature is missing. OpenRouter
    returns ``reasoning_details`` and asks for it back. OpenAI sends neither, so for
    OpenAI this is the same message as before.
    """
    raw: dict[str, Any] = {key: message[key] for key in ASSISTANT_FIELDS if key in message}
    raw["role"] = "assistant"
    raw.setdefault("content", None)
    tool_calls = []
    for call, source in calls:
        entry = {key: source[key] for key in TOOL_CALL_FIELDS if key in source}
        entry.update(
            id=call.id,  # the id the tool result will answer, even if we had to make one up
            type="function",
            function={"name": call.name, "arguments": call.arguments},
        )
        tool_calls.append(entry)
    if tool_calls:
        raw["tool_calls"] = tool_calls
    else:
        raw.pop("tool_calls", None)
    return raw


def _fallback_helps(exc: Exception) -> bool:
    """Whether trying LLM_MODEL_FALLBACK could succeed where the main model failed."""
    if is_key_rejected(exc) or isinstance(exc, openai.PermissionDeniedError):
        return False  # same key, same refusal
    if isinstance(exc, openai.BadRequestError | openai.UnprocessableEntityError):
        return False  # our request is the problem, not the model
    return isinstance(exc, openai.APIError)


class OpenAIChatModel:
    """ChatModel over the OpenAI API, or any gateway that speaks it."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: "httpx2.AsyncClient | None" = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        problem = chat_model_problem(settings)
        if problem:
            raise LLMError(problem, kind="not_configured")
        endpoint = resolve_endpoint(settings, "chat")
        if endpoint is None:  # chat_model_problem already said so; this satisfies type checks
            raise LLMError("No language model is configured.", kind="not_configured")
        self.endpoint = endpoint
        fast, smart = settings.llm_model_fast.strip(), settings.llm_model_smart.strip()
        self._models: dict[Tier, str] = {"fast": fast or smart, "smart": smart or fast}
        self._fallback = settings.llm_model_fallback.strip()
        self._mode = settings.llm_structured_output
        self._extra: dict[str, Any] = {}
        if settings.llm_reasoning_effort.strip():
            self._extra["reasoning_effort"] = settings.llm_reasoning_effort.strip()
        self._json_object_only: set[str] = set()  # models that refused strict schemas
        if client is None:
            client = make_client(
                endpoint,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
                http_client=http_client,
            )
            if settings.langsmith_tracing and settings.langsmith_api_key:
                from langsmith.wrappers import wrap_openai

                client = wrap_openai(client)
        self._client = client

    def model_for(self, tier: Tier) -> str:
        return self._models[tier]

    # ------------------------------------------------------------ plumbing

    def _error(self, exc: Exception, model: str) -> LLMError:
        failure = explain_error(exc, self.endpoint, doing="chat", model=model)
        rejected = isinstance(exc, openai.BadRequestError) and not is_key_rejected(exc)
        kind: ErrorKind = "rejected" if rejected else "unavailable"
        return LLMError(failure.message, kind=kind, retryable=failure.retryable, cause=exc)

    async def _complete(self, tier: Tier, **kwargs: Any) -> tuple[Any, LLMUsage]:
        """One chat completion, with the fallback model if the main one fails."""
        model = self._models[tier]
        started = time.perf_counter()
        fell_back = False
        try:
            completion = await self._client.chat.completions.create(
                model=model, **self._extra, **kwargs
            )
        except openai.APIError as exc:
            if not (self._fallback and self._fallback != model and _fallback_helps(exc)):
                raise self._error(exc, model) from exc
            logger.warning(
                "llm_fallback", model=model, fallback=self._fallback, error=type(exc).__name__
            )
            model, fell_back = self._fallback, True
            try:
                completion = await self._client.chat.completions.create(
                    model=model, **self._extra, **kwargs
                )
            except openai.APIError as second:
                raise self._error(second, model) from second
        usage = getattr(completion, "usage", None)
        return completion, LLMUsage(
            model=getattr(completion, "model", None) or model,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            calls=1,
            fallback_used=fell_back,
        )

    @staticmethod
    def _first_message(completion: Any) -> Any:
        choices = getattr(completion, "choices", None) or []
        if not choices:
            raise LLMError("The model sent an empty reply.", kind="malformed_output")
        return choices[0].message

    def _request_format(
        self, schema: type[BaseModel], system: str, mode: str
    ) -> tuple[dict[str, Any], str]:
        if mode == "json_schema":
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": to_strict_json_schema(schema),
                    "strict": True,
                },
            }, system
        # JSON mode only promises "some JSON object", so the schema goes in the prompt.
        schema_text = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        return {"type": "json_object"}, (
            f"{system}\n\n## Reply format\nReply with one JSON object, and nothing else, "
            f"that matches this JSON schema:\n{schema_text}"
        )

    # ------------------------------------------------------------ public API

    async def structured(
        self,
        schema: type[T],
        *,
        system: str,
        messages: Sequence[dict[str, Any]],
        tier: Tier,
        max_repairs: int = 1,
    ) -> StructuredReply[T]:
        model = self._models[tier]
        use_schema = self._mode == "json_schema" or (
            self._mode == "auto" and model not in self._json_object_only
        )
        mode: Literal["json_schema", "json_object"] = "json_schema" if use_schema else "json_object"
        conversation = list(messages)
        usage = LLMUsage(model=model)
        repairs = 0
        downgraded = False
        while True:
            response_format, system_text = self._request_format(schema, system, mode)
            try:
                completion, call_usage = await self._complete(
                    tier,
                    messages=[{"role": "system", "content": system_text}, *conversation],
                    response_format=response_format,
                )
            except LLMError as err:
                downgrade = mode == "json_schema" and self._mode == "auto"
                if downgrade and _request_rejected(err.cause):
                    logger.warning("llm_json_schema_unsupported", model=model, detail=err.message)
                    self._json_object_only.add(model)
                    mode, downgraded = "json_object", True
                    continue
                if downgraded and _request_rejected(err.cause):
                    # JSON mode was refused as well, so the format was not the problem.
                    self._json_object_only.discard(model)
                raise
            usage = usage + call_usage
            message = self._first_message(completion)
            refusal = getattr(message, "refusal", None)
            if refusal:
                raise LLMError(f"The model declined to answer: {refusal[:200]}", kind="refused")
            text = message.content or ""
            try:
                value = schema.model_validate_json(_strip_to_json(text))
            except (ValidationError, ValueError) as exc:
                if repairs >= max_repairs:
                    raise LLMError(
                        f"{self.endpoint.where}: the model's answer did not match the expected "
                        f"format ({schema.__name__}) after {repairs + 1} tries: {_problems(exc)}. "
                        "A stronger model, or one that supports structured output, may help.",
                        kind="malformed_output",
                        retryable=True,
                    ) from exc
                repairs += 1
                conversation += [
                    {"role": "assistant", "content": text[:4000]},
                    {"role": "user", "content": REPAIR_PROMPT.format(problems=_problems(exc))},
                ]
                continue
            return StructuredReply(value=value, usage=usage, mode=mode)

    async def tool_round(
        self,
        *,
        system: str,
        messages: Sequence[dict[str, Any]],
        tools: list[dict[str, Any]],
        tier: Tier,
    ) -> ToolRoundReply:
        completion, usage = await self._complete(
            tier,
            messages=[{"role": "system", "content": system}, *messages],
            tools=portable_tools(tools),
            tool_choice="auto",
        )
        message = self._first_message(completion)
        pairs: list[tuple[ToolCallRequest, dict[str, Any]]] = []
        for index, call in enumerate(getattr(message, "tool_calls", None) or []):
            function = getattr(call, "function", None)
            if function is None or not getattr(function, "name", None):
                continue
            request = ToolCallRequest(
                id=getattr(call, "id", None) or f"call_{index}",
                name=function.name,
                arguments=function.arguments or "{}",
            )
            pairs.append((request, _dump(call)))
        return ToolRoundReply(
            content=message.content,
            tool_calls=[request for request, _ in pairs],
            usage=usage,
            raw_message=_raw_assistant_message(_dump(message), pairs),
        )
