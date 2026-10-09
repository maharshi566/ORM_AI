"""The client the agents use to talk to a language model.

``llm_service`` decides where calls go (OpenAI, OmniRoute, OpenRouter, ...) and turns
SDK errors into plain sentences. This module adds what the agents need on top:

* **Two tiers.** ``fast`` (LLM_MODEL_FAST) for triage and tool selection, ``smart``
  (LLM_MODEL_SMART) for investigation and the final answer. If only one is set, both
  tiers use it.
* **Structured output.** ``structured()`` returns a validated Pydantic object, never
  free text. It asks for strict JSON-schema output first. A gateway or model that
  does not support that gets plain JSON mode with the schema written into the
  prompt instead (remembered per model). If the reply still does not match the
  schema, the model is shown what was wrong and asked once more; after that the
  call fails with ``LLMError(kind="malformed_output")`` rather than guessing.
* **Tool calling.** ``tool_round()`` runs one step: the model either asks for tool
  calls or answers. The agent runs the tools itself (through the ToolRegistry), so
  the model can never invent a tool result.
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
from app.services.llm_service import LLMConfigError, explain_error, make_client, resolve_endpoint

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

    def assistant_message(self) -> dict[str, Any]:
        """The model's turn, to send back with the tool results in the next round."""
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


def _format_unsupported(exc: Exception | None) -> bool:
    """True when the server rejected strict JSON-schema output itself."""
    if not isinstance(exc, openai.BadRequestError | openai.UnprocessableEntityError):
        return False
    text = str(exc).lower()
    return any(word in text for word in ("response_format", "json_schema", "structured output"))


def _fallback_helps(exc: Exception) -> bool:
    """Whether trying LLM_MODEL_FALLBACK could succeed where the main model failed."""
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
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
        kind: ErrorKind = "rejected" if isinstance(exc, openai.BadRequestError) else "unavailable"
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
                if downgrade and _format_unsupported(err.cause):
                    logger.warning("llm_json_schema_unsupported", model=model, detail=err.message)
                    self._json_object_only.add(model)
                    mode = "json_object"
                    continue
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
            tools=tools,
            tool_choice="auto",
        )
        message = self._first_message(completion)
        calls = []
        for index, call in enumerate(getattr(message, "tool_calls", None) or []):
            function = getattr(call, "function", None)
            if function is None or not getattr(function, "name", None):
                continue
            calls.append(
                ToolCallRequest(
                    id=getattr(call, "id", None) or f"call_{index}",
                    name=function.name,
                    arguments=function.arguments or "{}",
                )
            )
        return ToolRoundReply(content=message.content, tool_calls=calls, usage=usage)
