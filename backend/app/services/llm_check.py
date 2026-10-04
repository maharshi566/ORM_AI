"""Checks that a model endpoint works the way this app needs it to.

``python -m scripts.check_llm`` runs these probes against the configured chat and
embedding endpoints (OpenAI, or a gateway such as OmniRoute) and prints one line each:

    Model list    the endpoint answers and lists its models
    Chat          a model replies to a one-line prompt
    JSON mode     the model can be told to answer in JSON    (RERANKER=llm needs it)
    Tool calling  the model can call a tool                  (the Phase 4 agents need it)
    Embeddings    the embedding model returns vectors        (knowledge search needs it)

Each probe makes one tiny request with no retries, so a problem shows up at once. A
failing probe never stops the others, and nothing here prints a key.
"""

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import openai
from openai import AsyncOpenAI

from app.config.settings import Settings
from app.services.llm_service import (
    LLMConfigError,
    LLMEndpoint,
    explain_error,
    is_remote_plain_http,
    make_client,
    resolve_endpoint,
)

if TYPE_CHECKING:
    import httpx2

Status = Literal["ok", "warn", "fail", "skip"]
Outcome = tuple[Status, str, str]  # status, what happened, what to try

TOOL = {
    "type": "function",
    "function": {
        "name": "get_stock",
        "description": "Look up how many units of a product the shop has in stock.",
        "parameters": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "Product code, for example KIR-001"}
            },
            "required": ["sku"],
        },
    },
}

JSON_ADVICE = "Keep RERANKER=heuristic, or choose a model that supports JSON mode."
TOOLS_ADVICE = "The Phase 4 agents depend on tool calling: choose a model that supports it."

# What can be wrong with a reply that is not shaped like an OpenAI-style answer.
_BAD_SHAPE = (AttributeError, IndexError, KeyError, TypeError, ValueError)


@dataclass(frozen=True, slots=True)
class Probe:
    name: str
    status: Status
    detail: str
    seconds: float = 0.0
    hint: str = ""
    unreachable: bool = False  # the endpoint did not answer at all


@dataclass(slots=True)
class CheckReport:
    chat: LLMEndpoint | None = None
    embeddings: LLMEndpoint | None = None
    embedding_model: str = ""
    probes: list[Probe] = field(default_factory=list)
    config_error: str = ""

    @property
    def exit_code(self) -> int:
        """0: fine (warnings allowed), 1: something failed, 2: nothing to check."""
        if self.config_error or not self.probes:
            return 2
        return 1 if any(probe.status == "fail" for probe in self.probes) else 0


async def _guarded(
    name: str,
    endpoint: LLMEndpoint,
    *,
    doing: str,
    model: str,
    work: Callable[[], Awaitable[Outcome]],
    on_error: Status = "fail",
    error_hint: str = "",
) -> Probe:
    """Run one probe. Any failure becomes a Probe, never an exception."""
    started = time.perf_counter()
    unreachable = False
    try:
        status, detail, hint = await work()
    except openai.OpenAIError as exc:
        failure = explain_error(exc, endpoint, doing=doing, model=model)
        unreachable = isinstance(exc, openai.APIConnectionError)
        status = "fail" if unreachable else on_error
        detail, hint = failure.message, ("" if unreachable else error_hint)
    except _BAD_SHAPE as exc:
        status = on_error
        detail = (
            f"{endpoint.where} sent a reply this app cannot read ({type(exc).__name__}). "
            "Is the address an OpenAI-style endpoint (usually ending in /v1)?"
        )
        hint = ""
    elapsed = round(time.perf_counter() - started, 1)
    return Probe(name, status, detail, elapsed, hint, unreachable)


# ------------------------------------------------------------------- probes


async def _list_models(client: AsyncOpenAI, wanted: list[str]) -> tuple[Outcome, list[str]]:
    page = await client.models.list()
    ids = [model.id for model in page.data]
    if not ids:
        return (
            (
                "warn",
                "The endpoint answered but lists no models.",
                "Connect a provider in its dashboard.",
            ),
            ids,
        )
    detail = f"{len(ids)} models, for example {', '.join(ids[:4])}"
    missing = [name for name in wanted if name not in ids]
    if missing:
        detail += (
            f". Not in the list: {', '.join(missing)} (some gateways accept names they do "
            "not list; the Chat check decides)"
        )
        return ("warn", detail, ""), ids
    return ("ok", detail, ""), ids


async def _chat(client: AsyncOpenAI, model: str) -> Outcome:
    response = await client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": "Reply with the single word: ready"}]
    )
    text = (response.choices[0].message.content or "").strip()
    if not text:
        return "warn", f"{model} answered with an empty reply.", "Try another model."
    return "ok", f"{model} replied {text[:40]!r}", ""


async def _json_mode(client: AsyncOpenAI, model: str) -> Outcome:
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You answer with one JSON object and nothing else."},
            {"role": "user", "content": 'Return a JSON object with the key "status" set to "ok".'},
        ],
        response_format={"type": "json_object"},
    )
    text = (response.choices[0].message.content or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return (
            "warn",
            f"{model} did not return a JSON object ({text[:40]!r}).",
            JSON_ADVICE,
        )
    return "ok", f"{model} returned valid JSON", ""


async def _tool_calling(client: AsyncOpenAI, model: str) -> Outcome:
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": "How many units of KIR-001 are in stock? Use the tool to find out.",
            }
        ],
        tools=[TOOL],
    )
    calls = response.choices[0].message.tool_calls or []
    function = getattr(calls[0], "function", None) if calls else None
    if function is None:
        return (
            "warn",
            f"{model} answered in text instead of calling the tool.",
            TOOLS_ADVICE,
        )
    try:
        arguments = json.loads(function.arguments)
    except json.JSONDecodeError:
        arguments = None
    if function.name != "get_stock" or not isinstance(arguments, dict) or "sku" not in arguments:
        return (
            "warn",
            f"{model} called a tool but with arguments this app cannot use.",
            TOOLS_ADVICE,
        )
    return "ok", f"{model} called get_stock(sku={arguments['sku']!r})", ""


async def _embeddings(client: AsyncOpenAI, model: str) -> Outcome:
    response = await client.embeddings.create(
        model=model, input=["household credit limit"], encoding_format="float"
    )
    vector = response.data[0].embedding
    if not vector:
        return "fail", f"{model} returned an empty vector.", ""
    return "ok", f"{model} returned {len(vector)} numbers per text", ""


def _embedding_hint(listed: list[str]) -> str:
    found = [name for name in listed if "embed" in name.lower()]
    if not found:
        return ""
    return f"Models with 'embed' in the name here: {', '.join(found[:6])}"


# ------------------------------------------------------------------- runner


def _models_to_test(settings: Settings, models: list[str] | None) -> list[str]:
    wanted = models or [settings.llm_model_fast, settings.llm_model_smart]
    unique: list[str] = []
    for name in (item.strip() for item in wanted):
        if name and name not in unique:
            unique.append(name)
    return unique


async def run_checks(
    settings: Settings,
    *,
    models: list[str] | None = None,
    embedding_model: str | None = None,
    http_client: "httpx2.AsyncClient | None" = None,
) -> CheckReport:
    """Probe the chat and embedding endpoints. ``http_client`` is for tests."""
    report = CheckReport(embedding_model=(embedding_model or settings.embedding_model).strip())
    try:
        report.chat = resolve_endpoint(settings, "chat")
        report.embeddings = resolve_endpoint(settings, "embeddings")
    except LLMConfigError as exc:
        report.config_error = str(exc)
        return report

    offline = report.embedding_model == "hash" or report.embedding_model.startswith("hash-")
    if report.chat is None and (report.embeddings is None or offline):
        return report  # nothing configured: the caller explains how to set it up

    def client_for(endpoint: LLMEndpoint) -> AsyncOpenAI:
        return make_client(
            endpoint,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,  # a diagnostic should show the first failure, not hide it
            http_client=http_client,
        )

    clients: list[AsyncOpenAI] = []
    listed: list[str] = []
    try:
        if report.chat is not None:
            chat_client = client_for(report.chat)
            clients.append(chat_client)
            listed = await _check_chat(report, report.chat, chat_client, settings, models)
        else:
            report.probes.append(
                Probe("Chat", "skip", "No chat endpoint: set OPENAI_API_KEY or LLM_BASE_URL.")
            )
        if offline:
            report.probes.append(
                Probe("Embeddings", "skip", f"{report.embedding_model} is the offline embedder.")
            )
        elif report.embeddings is None:
            report.probes.append(
                Probe(
                    "Embeddings",
                    "fail",
                    f"EMBEDDING_MODEL is {report.embedding_model}, which needs OPENAI_API_KEY "
                    "or a gateway (LLM_BASE_URL).",
                    hint="Or set EMBEDDING_MODEL=hash to search offline.",
                )
            )
        else:
            embedding_client = client_for(report.embeddings)
            clients.append(embedding_client)
            same_place = (
                report.chat is not None and report.chat.base_url == report.embeddings.base_url
            )
            probe = await _guarded(
                "Embeddings",
                report.embeddings,
                doing="embeddings",
                model=report.embedding_model,
                work=lambda: _embeddings(embedding_client, report.embedding_model),
            )
            if probe.status == "fail" and same_place and not probe.hint:
                probe = Probe(
                    probe.name, probe.status, probe.detail, probe.seconds, _embedding_hint(listed)
                )
            report.probes.append(probe)
        for endpoint in {e.base_url: e for e in (report.chat, report.embeddings) if e}.values():
            if is_remote_plain_http(endpoint):
                report.probes.append(
                    Probe(
                        "Connection security",
                        "warn",
                        f"{endpoint.where} uses http:// across the internet, so the key and "
                        "the shop data travel unencrypted.",
                        hint="Use an https:// address, or keep the gateway on this computer.",
                    )
                )
    finally:
        if http_client is None:  # an injected client belongs to the caller
            for client in clients:
                await client.close()
    return report


async def _check_chat(
    report: CheckReport,
    endpoint: LLMEndpoint,
    client: AsyncOpenAI,
    settings: Settings,
    models: list[str] | None,
) -> list[str]:
    """The chat-side probes. Returns the model ids the endpoint listed."""
    names = _models_to_test(settings, models)
    listed: list[str] = []

    async def listing() -> Outcome:
        outcome, ids = await _list_models(client, names)
        listed.extend(ids)
        return outcome

    first = await _guarded(
        "Model list", endpoint, doing="model list", model="", work=listing, on_error="warn"
    )
    report.probes.append(first)
    if first.unreachable:
        report.probes.append(Probe("Chat", "skip", "Skipped: the endpoint could not be reached."))
        return listed
    if not names:
        report.probes.append(
            Probe(
                "Chat",
                "skip",
                "No chat model to test.",
                hint="Set LLM_MODEL_FAST in .env, or pass --model NAME.",
            )
        )
        return listed
    for name in names:
        chat = await _guarded(
            "Chat", endpoint, doing="chat", model=name, work=lambda n=name: _chat(client, n)
        )
        report.probes.append(chat)
        if chat.status == "fail":
            continue  # JSON and tool checks would only repeat the same error
        report.probes.append(
            await _guarded(
                "JSON mode",
                endpoint,
                doing="chat (JSON mode)",
                model=name,
                work=lambda n=name: _json_mode(client, n),
                on_error="warn",
                error_hint=JSON_ADVICE,
            )
        )
        report.probes.append(
            await _guarded(
                "Tool calling",
                endpoint,
                doing="chat (tool calling)",
                model=name,
                work=lambda n=name: _tool_calling(client, n),
                on_error="warn",
                error_hint=TOOLS_ADVICE,
            )
        )
    return listed


async def list_model_ids(
    settings: Settings, *, contains: str = "", http_client: "httpx2.AsyncClient | None" = None
) -> tuple[list[str], str]:
    """(model ids, problem). The ids are sorted and filtered by ``contains`` (any case).

    ``problem`` is empty on success and a plain-English sentence otherwise. This is what
    ``python -m scripts.check_llm --list`` prints, so a beginner can copy a model name
    into LLM_MODEL_FAST or EMBEDDING_MODEL.
    """
    try:
        endpoint = resolve_endpoint(settings, "chat")
    except LLMConfigError as exc:
        return [], str(exc)
    if endpoint is None:
        return [], "Nothing is configured: set OPENAI_API_KEY, or LLM_BASE_URL for a gateway."
    client = make_client(
        endpoint, timeout=settings.llm_timeout_seconds, max_retries=0, http_client=http_client
    )
    try:
        page = await client.models.list()
        ids = sorted(model.id for model in page.data)
    except openai.OpenAIError as exc:
        return [], explain_error(exc, endpoint, doing="model list").message
    except _BAD_SHAPE:
        return [], f"{endpoint.where} sent a model list this app cannot read."
    finally:
        if http_client is None:
            await client.close()
    needle = contains.strip().lower()
    return [name for name in ids if needle in name.lower()], ""


# ------------------------------------------------------------------- output

_LABEL = {"ok": " ok ", "warn": "WARN", "fail": "FAIL", "skip": "skip"}


def _describe(endpoint: LLMEndpoint | None) -> str:
    if endpoint is None:
        return "not configured (no OPENAI_API_KEY and no LLM_BASE_URL)"
    key = f"key from {endpoint.key_source}" if endpoint.has_key else "no key set"
    return f"{endpoint.where} ({key})"


def format_report(report: CheckReport) -> str:
    """The text the check script prints. It never contains a key."""
    if report.config_error:
        return f"Cannot check: {report.config_error}"
    lines = [
        f"Chat:        {_describe(report.chat)}",
        f"Embeddings:  {_describe(report.embeddings)}  model: {report.embedding_model}",
    ]
    if not report.probes:
        lines += [
            "",
            "Nothing to check. Set one of these in .env:",
            "  OPENAI_API_KEY=...            to use OpenAI",
            "  LLM_BASE_URL=http://localhost:20128/v1   to use a gateway such as OmniRoute",
        ]
        return "\n".join(lines)
    lines.append("")
    for probe in report.probes:
        lines.append(f"[{_LABEL[probe.status]}] {probe.name:<20} {probe.detail}" + _took(probe))
        if probe.hint:
            lines.append(f"       -> {probe.hint}")
    lines.append("")
    if any(probe.status == "fail" for probe in report.probes):
        lines.append("Something failed. Fix the FAIL lines above and run this again.")
    elif any(probe.status == "warn" for probe in report.probes):
        lines.append("It works, with warnings. Read the WARN lines before relying on it.")
    else:
        lines.append("Everything passed.")
    return "\n".join(lines)


def _took(probe: Probe) -> str:
    return f"  ({probe.seconds:.1f} s)" if probe.status != "skip" and probe.seconds else ""
