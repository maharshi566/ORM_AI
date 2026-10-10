"""Using an OpenAI-compatible gateway such as OmniRoute.

Covers choosing the endpoint from settings, the embedding and reranking clients, the
error messages, and the ``check_llm`` script. A fake gateway (tests/fake_gateway.py)
stands in for the real service, reached through the real OpenAI SDK, so these tests
prove what the app sends (address, key, JSON mode, tools) and how it reads the answers.
"""

import httpx2
import openai
import pytest

from app.config.settings import Settings
from app.rag.embeddings import (
    EmbeddingConfigError,
    EmbeddingError,
    OpenAIEmbedder,
    build_embedder,
)
from app.rag.reranker import HeuristicReranker, LLMReranker, build_reranker
from app.rag.retriever import RetrievedChunk
from app.services.llm_check import CheckReport, format_report, list_model_ids, run_checks
from app.services.llm_service import (
    PLACEHOLDER_KEY,
    LLMConfigError,
    LLMEndpoint,
    explain_error,
    is_remote_plain_http,
    make_client,
    resolve_endpoint,
)
from scripts import check_llm
from tests.fake_gateway import BASE_URL, FakeGateway, unreachable_client

KEY = "sk-secret-key-123"

# Real environment variables could otherwise leak into a test through Settings.
BLANK = {
    "openai_api_key": None,
    "llm_base_url": "",
    "llm_api_key": None,
    "embedding_base_url": "",
    "embedding_api_key": None,
    "llm_model_fast": "",
    "llm_model_smart": "",
}


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **{**BLANK, **values})  # type: ignore[arg-type]


def gateway_settings(**values: object) -> Settings:
    defaults = {
        "llm_base_url": BASE_URL,
        "llm_api_key": KEY,
        "llm_model_fast": "auto/fast",
        "llm_model_smart": "auto/smart",
        "embedding_model": "fake/embed-small",
    }
    return settings(**{**defaults, **values})


# ------------------------------------------------------------ choosing the endpoint


def test_nothing_configured_means_no_endpoint() -> None:
    assert resolve_endpoint(settings(), "chat") is None
    assert resolve_endpoint(settings(), "embeddings") is None


def test_an_openai_key_alone_means_openai() -> None:
    endpoint = resolve_endpoint(settings(openai_api_key="sk-abc"))

    assert endpoint is not None
    assert endpoint.base_url is None and not endpoint.is_gateway
    assert endpoint.where == "OpenAI" and endpoint.key_source == "OPENAI_API_KEY"


def test_a_gateway_needs_no_key_until_it_asks_for_one() -> None:
    endpoint = resolve_endpoint(settings(llm_base_url=" http://localhost:20128/v1/ "))

    assert endpoint is not None and endpoint.is_gateway
    assert endpoint.base_url == "http://localhost:20128/v1"  # spaces and the final slash go
    assert not endpoint.has_key and endpoint.api_key == PLACEHOLDER_KEY


def test_the_gateway_key_wins_over_the_openai_key() -> None:
    both = resolve_endpoint(settings(llm_base_url=BASE_URL, llm_api_key="gw", openai_api_key="sk"))
    only_openai = resolve_endpoint(settings(llm_base_url=BASE_URL, openai_api_key="sk"))

    assert both is not None and (both.api_key, both.key_source) == ("gw", "LLM_API_KEY")
    assert only_openai is not None
    assert (only_openai.api_key, only_openai.key_source) == ("sk", "OPENAI_API_KEY")


def test_embeddings_follow_chat_unless_told_otherwise() -> None:
    together = settings(llm_base_url=BASE_URL, llm_api_key="gw")
    chat, embeddings = resolve_endpoint(together, "chat"), resolve_endpoint(together, "embeddings")
    assert chat is not None and embeddings is not None
    assert embeddings.base_url == chat.base_url and embeddings.api_key == "gw"

    own_key = settings(llm_base_url=BASE_URL, llm_api_key="gw", embedding_api_key="emb")
    assert resolve_endpoint(own_key, "embeddings").api_key == "emb"  # type: ignore[union-attr]
    assert resolve_endpoint(own_key, "chat").api_key == "gw"  # type: ignore[union-attr]


def test_a_separate_embedding_service_never_gets_the_gateways_key() -> None:
    # Chat through the gateway, embeddings straight from OpenAI.
    split = settings(
        llm_base_url=BASE_URL,
        llm_api_key="gateway-key",
        openai_api_key="sk-openai",
        embedding_base_url="https://api.openai.com/v1",
    )
    embeddings = resolve_endpoint(split, "embeddings")
    assert embeddings is not None
    assert (embeddings.base_url, embeddings.api_key) == (None, "sk-openai")  # plain OpenAI

    # A second gateway with no key of its own gets no key, not the first one's.
    other = settings(
        llm_base_url=BASE_URL,
        llm_api_key="gateway-key",
        embedding_base_url="http://other.local:9000/v1",
    )
    embeddings = resolve_endpoint(other, "embeddings")
    assert embeddings is not None and embeddings.base_url == "http://other.local:9000/v1"
    assert embeddings.api_key == PLACEHOLDER_KEY and not embeddings.has_key


@pytest.mark.parametrize("bad", ["localhost:20128/v1", "ftp://host/v1", "http://", "20128"])
def test_a_base_url_that_is_not_a_web_address_is_rejected(bad: str) -> None:
    with pytest.raises(LLMConfigError, match=r"http://.*localhost:20128/v1"):
        resolve_endpoint(settings(llm_base_url=bad))


def test_passwords_and_keys_are_never_shown() -> None:
    url = "http://admin:hunter2@localhost:20128/v1"
    endpoint = resolve_endpoint(settings(llm_base_url=url, llm_api_key=KEY))

    assert endpoint is not None and endpoint.base_url == url  # still used to connect
    for shown in (endpoint.where, repr(endpoint), str(endpoint)):
        assert "hunter2" not in shown and KEY not in shown
    with pytest.raises(LLMConfigError) as excinfo:
        resolve_endpoint(settings(llm_base_url="ftp://admin:hunter2@host/v1"))
    assert "hunter2" not in str(excinfo.value)


def test_the_key_settings_are_secret_strings() -> None:
    text = repr(settings(llm_api_key=KEY, embedding_api_key=KEY, openai_api_key=KEY))
    assert KEY not in text


@pytest.mark.parametrize(
    ("url", "risky"),
    [
        ("http://localhost:20128/v1", False),
        ("http://127.0.0.1:20128/v1", False),
        ("http://[::1]:20128/v1", False),
        ("http://omniroute:20128/v1", False),  # a Docker service name
        ("http://host.docker.internal:20128/v1", False),
        ("http://192.168.1.5:20128/v1", False),
        ("http://10.0.0.2/v1", False),
        ("https://gateway.example.com/v1", False),  # encrypted
        ("http://gateway.example.com/v1", True),
        ("http://8.8.8.8/v1", True),
    ],
)
def test_only_plain_http_to_the_internet_is_flagged(url: str, risky: bool) -> None:
    endpoint = resolve_endpoint(settings(llm_base_url=url))
    assert endpoint is not None and is_remote_plain_http(endpoint) is risky


def test_openai_itself_is_never_flagged() -> None:
    endpoint = resolve_endpoint(settings(openai_api_key="sk"))
    assert endpoint is not None and not is_remote_plain_http(endpoint)


def test_gateway_settings_are_read_from_the_env_file(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"  # type: ignore[operator]
    env.write_text(
        "LLM_BASE_URL=http://localhost:20128/v1\nLLM_API_KEY=sk-from-file\n"
        "EMBEDDING_BASE_URL=\nEMBEDDING_API_KEY=\n",
        encoding="utf-8",
    )

    loaded = Settings(_env_file=env)

    assert loaded.llm_base_url == "http://localhost:20128/v1"
    assert (
        loaded.llm_api_key is not None and loaded.llm_api_key.get_secret_value() == "sk-from-file"
    )
    assert loaded.embedding_base_url == "" and loaded.embedding_api_key is None  # blank = unset


# ------------------------------------------------------------ the embedding client


def test_the_factory_builds_a_gateway_embedder() -> None:
    embedder = build_embedder(gateway_settings())

    assert isinstance(embedder, OpenAIEmbedder) and embedder.model == "fake/embed-small"
    assert embedder.endpoint.where == f"the gateway at {BASE_URL}"


def test_the_missing_setup_message_names_both_ways_out() -> None:
    with pytest.raises(EmbeddingConfigError) as excinfo:
        build_embedder(settings(embedding_model="text-embedding-3-small"))

    message = str(excinfo.value)
    assert "OPENAI_API_KEY" in message and "LLM_BASE_URL" in message and "hash" in message


def test_a_bad_gateway_address_is_a_config_error_for_embeddings() -> None:
    with pytest.raises(EmbeddingConfigError, match="http://"):
        build_embedder(settings(embedding_model="some-model", llm_base_url="localhost:20128"))


def embedder_for(gateway: FakeGateway, **values: object) -> OpenAIEmbedder:
    config = gateway_settings(**values)
    endpoint = resolve_endpoint(config, "embeddings")
    assert endpoint is not None
    client = make_client(endpoint, timeout=5, max_retries=0, http_client=gateway.client())
    return OpenAIEmbedder(config.embedding_model, endpoint, client=client)


async def test_embeddings_reach_the_gateway_with_its_key_and_float_format() -> None:
    gateway = FakeGateway(api_key=KEY)

    vectors = await embedder_for(gateway).embed_documents(["abc", "", "defgh"])

    assert [vector[0] for vector in vectors] == [3.0, 1.0, 5.0]  # "" is sent as " "
    (call,) = gateway.sent_to("/v1/embeddings")
    assert call.authorization == f"Bearer {KEY}"
    assert call.body["model"] == "fake/embed-small"
    assert call.body["encoding_format"] == "float"  # not the SDK's base64 default


@pytest.mark.parametrize(
    ("gateway_options", "expected", "retryable"),
    [
        ({"api_key": "another-key"}, ["rejected the API key", "LLM_API_KEY", "dashboard"], False),
        ({"embedding_models": ()}, ["HTTP 404", "ends in /v1", "fake/embed-small"], False),
        ({"fail": {"/v1/embeddings": 503}}, ["HTTP 503", "Upstream provider failed"], True),
        ({"fail": {"/v1/embeddings": 429}}, ["rate limit", "free quota"], True),
    ],
    ids=["bad key", "unknown model", "server error", "rate limit"],
)
async def test_gateway_problems_become_clear_embedding_errors(
    gateway_options: dict[str, object], expected: list[str], retryable: bool
) -> None:
    embedder = embedder_for(FakeGateway(**gateway_options))  # type: ignore[arg-type]

    with pytest.raises(EmbeddingError) as excinfo:
        await embedder.embed_query("credit limit")

    message = str(excinfo.value)
    assert f"the gateway at {BASE_URL}" in message
    for fragment in expected:
        assert fragment in message
    assert KEY not in message
    assert excinfo.value.retryable is retryable


async def test_a_gateway_that_is_not_running_says_so() -> None:
    config = gateway_settings()
    endpoint = resolve_endpoint(config, "embeddings")
    assert endpoint is not None
    client = make_client(endpoint, timeout=5, max_retries=0, http_client=unreachable_client())

    with pytest.raises(EmbeddingError, match="Could not reach the gateway") as excinfo:
        await OpenAIEmbedder("fake/embed-small", endpoint, client=client).embed_query("x")

    assert "Is it running?" in str(excinfo.value) and excinfo.value.retryable


def test_error_text_for_unusual_failures_is_still_plain() -> None:
    endpoint = LLMEndpoint("chat", None, "sk-x", "OPENAI_API_KEY")

    generic = explain_error(openai.OpenAIError("boom"), endpoint, doing="chat")

    assert generic.message == "OpenAI: chat failed (OpenAIError)." and not generic.retryable


def _status_error(cls: type[openai.APIStatusError], status: int, message: str) -> Exception:
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx2.Response(status, request=request)
    return cls(message, response=response, body={"message": message})


LONG_KEY = "sk-proj-" + "a" * 40 + "WXYZ"


def test_the_report_says_which_key_and_where_it_came_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from_file = resolve_endpoint(settings(openai_api_key=LONG_KEY))
    monkeypatch.setenv("OPENAI_API_KEY", LONG_KEY)
    from_variable = resolve_endpoint(Settings(_env_file=None))

    assert from_file.key_hint == from_variable.key_hint == "sk-proj-…WXYZ"
    assert from_file.key_origin == "OPENAI_API_KEY in .env"
    assert not from_file.key_from_environment and from_variable.key_from_environment
    report = format_report(CheckReport(embedding_model="hash", chat=from_variable))
    assert "key sk-proj-…WXYZ from the OPENAI_API_KEY environment variable" in report
    assert "wins over .env" in report and LONG_KEY not in report


def test_a_rejected_key_says_where_it_came_from(monkeypatch: pytest.MonkeyPatch) -> None:
    rejected = _status_error(openai.AuthenticationError, 401, f"Incorrect API key {LONG_KEY}")
    in_file = explain_error(
        rejected, resolve_endpoint(settings(openai_api_key=LONG_KEY)), doing="chat"
    )
    monkeypatch.setenv("OPENAI_API_KEY", LONG_KEY)
    in_variable = explain_error(rejected, resolve_endpoint(Settings(_env_file=None)), doing="chat")

    assert "platform.openai.com/api-keys" in in_file.message
    assert "environment variable" in in_variable.message
    assert "wins over .env" in in_variable.message
    assert LONG_KEY not in in_file.message + in_variable.message


def test_a_refused_request_is_not_mistaken_for_a_bad_key() -> None:
    refused = _status_error(
        openai.PermissionDeniedError, 403, f"Missing scopes: model.request for {LONG_KEY}"
    )

    failure = explain_error(
        refused, resolve_endpoint(settings(openai_api_key=LONG_KEY)), doing="chat"
    )

    assert "accepted the API key" in failure.message and "HTTP 403" in failure.message
    assert "Missing scopes: model.request" in failure.message
    assert "Permissions to All" in failure.message
    assert LONG_KEY not in failure.message and "[key]" in failure.message


def test_an_account_without_credit_is_not_called_a_rate_limit() -> None:
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    no_credit = openai.RateLimitError(
        "You exceeded your current quota",
        response=httpx2.Response(429, request=request),
        body={"message": "You exceeded your current quota", "code": "insufficient_quota"},
    )
    busy = _status_error(openai.RateLimitError, 429, "Rate limit reached for requests")
    endpoint = resolve_endpoint(settings(openai_api_key=LONG_KEY))

    credit = explain_error(no_credit, endpoint, doing="chat")
    limit = explain_error(busy, endpoint, doing="chat")

    assert "no API credit" in credit.message and "Billing" in credit.message
    assert not credit.retryable  # waiting does not help; adding credit does
    assert "rate limit" in limit.message and limit.retryable


# ------------------------------------------------------------ the reranker


def make_chunk(section: str, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        id=section, citation=f"[X v1 §{section}]", document_id="X", document_key="X@v1",
        title="Doc", version=1, status="current", category="policy", section=section,
        page=None, effective_date="2026-01-01", source="internal", trust="trusted",
        shop_id=None, text=f"Text about {section}.", fused_score=score, score=score,
    )  # fmt: skip


async def test_the_llm_reranker_works_through_a_gateway() -> None:
    gateway = FakeGateway(api_key=KEY)
    reranker = build_reranker(gateway_settings(reranker="llm"), http_client=gateway.client())

    assert isinstance(reranker, LLMReranker)
    ranked = await reranker.rerank("q", [make_chunk("A", 0.9), make_chunk("B", 0.5)], 2)

    assert [chunk.section for chunk in ranked] == ["B", "A"]  # the fake scores the last best
    (call,) = gateway.sent_to("/v1/chat/completions")
    assert call.authorization == f"Bearer {KEY}"
    assert call.body["model"] == "auto/fast"
    assert call.body["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    "values",
    [
        {"llm_model_fast": ""},  # no model named
        {"llm_base_url": "", "llm_api_key": None},  # no key and no gateway
        {"llm_base_url": "localhost:20128"},  # not a web address
    ],
    ids=["no model", "no endpoint", "bad address"],
)
def test_the_llm_reranker_falls_back_to_the_heuristic_when_it_cannot_be_built(
    values: dict[str, object],
) -> None:
    assert isinstance(build_reranker(gateway_settings(reranker="llm", **values)), HeuristicReranker)


# ------------------------------------------------------------ the check script


async def check(gateway: FakeGateway, **values: object) -> CheckReport:
    return await run_checks(gateway_settings(**values), http_client=gateway.client())


def outcome(report: CheckReport) -> list[tuple[str, str]]:
    return [(probe.name, probe.status) for probe in report.probes]


async def test_a_healthy_gateway_passes_every_check() -> None:
    gateway = FakeGateway(api_key=KEY)

    report = await check(gateway)

    assert outcome(report) == [
        ("Model list", "ok"),
        ("Chat", "ok"),
        ("JSON mode", "ok"),
        ("JSON schema", "ok"),
        ("Tool calling", "ok"),  # the four checks above are for auto/fast ...
        ("Chat", "ok"),
        ("JSON mode", "ok"),
        ("JSON schema", "ok"),
        ("Tool calling", "ok"),  # ... and these for auto/smart
        ("Embeddings", "ok"),
    ]
    assert report.exit_code == 0
    text = format_report(report)
    assert "Everything passed." in text
    assert f"the gateway at {BASE_URL} (key sk-…-123 from LLM_API_KEY in .env)" in text
    assert KEY not in text
    assert all(call.authorization == f"Bearer {KEY}" for call in gateway.requests)


async def test_missing_json_schema_support_is_a_warning_not_a_failure() -> None:
    report = await check(FakeGateway(api_key=KEY, json_schema=False))

    schema = [probe for probe in report.probes if probe.name == "JSON schema"]
    assert schema and all(probe.status == "warn" for probe in schema)
    assert "fall back to JSON mode" in schema[0].hint
    assert report.exit_code == 0


async def test_missing_json_mode_is_a_warning_with_advice() -> None:
    report = await check(FakeGateway(api_key=KEY, json_mode=False))

    json_checks = [probe for probe in report.probes if probe.name == "JSON mode"]
    assert json_checks and {probe.status for probe in json_checks} == {"warn"}
    assert all("RERANKER=heuristic" in probe.hint for probe in json_checks)
    assert report.exit_code == 0 and "with warnings" in format_report(report)


async def test_a_model_that_ignores_tools_is_a_warning() -> None:
    report = await check(FakeGateway(api_key=KEY, tool_calls=False))

    tools = [probe for probe in report.probes if probe.name == "Tool calling"]
    assert {probe.status for probe in tools} == {"warn"}
    assert "answered in text" in tools[0].detail and "Phase 4" in tools[0].hint
    assert report.exit_code == 0


async def test_a_rejected_key_fails_chat_and_skips_the_follow_up_checks() -> None:
    gateway = FakeGateway(api_key="the-real-key")  # the app sends a different one

    report = await check(gateway)

    chats = [probe for probe in report.probes if probe.name == "Chat"]
    assert [probe.status for probe in chats] == ["fail", "fail"]
    assert all("rejected the API key" in probe.detail for probe in chats)
    assert not [probe for probe in report.probes if probe.name in {"JSON mode", "Tool calling"}]
    assert report.exit_code == 1
    text = format_report(report)
    assert "Something failed" in text and KEY not in text and "the-real-key" not in text


async def test_an_unknown_model_names_the_model_and_the_url_hint() -> None:
    report = await check(FakeGateway(api_key=KEY), llm_model_fast="nope", llm_model_smart="")

    chat = next(probe for probe in report.probes if probe.name == "Chat")
    assert chat.status == "fail" and "'nope'" in chat.detail and "ends in /v1" in chat.detail
    assert next(p for p in report.probes if p.name == "Model list").status == "warn"
    assert report.exit_code == 1


async def test_an_unreachable_gateway_fails_once_and_skips_the_rest() -> None:
    report = await run_checks(gateway_settings(), http_client=unreachable_client())

    first, second = report.probes[:2]
    assert (first.name, first.status) == ("Model list", "fail")
    assert "Could not reach the gateway" in first.detail
    assert (second.name, second.status) == ("Chat", "skip")
    assert report.exit_code == 1


async def test_the_offline_embedder_needs_no_call() -> None:
    gateway = FakeGateway(api_key=KEY)

    report = await check(gateway, embedding_model="hash")

    assert ("Embeddings", "skip") in outcome(report) and not gateway.sent_to("/v1/embeddings")
    assert report.exit_code == 0


async def test_a_wrong_embedding_model_lists_the_ones_that_exist() -> None:
    report = await check(FakeGateway(api_key=KEY), embedding_model="nope/embed")

    probe = next(probe for probe in report.probes if probe.name == "Embeddings")
    assert probe.status == "fail" and "fake/embed-small" in probe.hint
    assert report.exit_code == 1


async def test_no_chat_model_named_is_skipped_not_failed() -> None:
    report = await check(FakeGateway(api_key=KEY), llm_model_fast="", llm_model_smart="")

    chat = next(probe for probe in report.probes if probe.name == "Chat")
    assert chat.status == "skip" and "LLM_MODEL_FAST" in chat.hint
    assert report.exit_code == 0


async def test_a_model_given_on_the_command_line_beats_the_settings() -> None:
    gateway = FakeGateway(api_key=KEY)

    report = await run_checks(gateway_settings(), models=["auto"], http_client=gateway.client())

    chat_calls = gateway.sent_to("/v1/chat/completions")
    assert {call.body["model"] for call in chat_calls} == {"auto"}
    assert report.exit_code == 0


async def test_plain_http_to_a_public_address_is_flagged_once() -> None:
    gateway = FakeGateway(api_key=KEY)
    config = gateway_settings(llm_base_url="http://gateway.example.com/v1")

    report = await run_checks(config, http_client=gateway.client())

    warnings = [probe for probe in report.probes if probe.name == "Connection security"]
    assert len(warnings) == 1 and "unencrypted" in warnings[0].detail
    assert report.exit_code == 0


async def test_nothing_configured_gives_setup_instructions() -> None:
    report = await run_checks(settings())

    assert report.exit_code == 2 and not report.probes
    text = format_report(report)
    assert "Nothing to check" in text and "OPENAI_API_KEY" in text and "LLM_BASE_URL" in text


async def test_a_bad_address_is_reported_without_any_request() -> None:
    report = await run_checks(settings(llm_base_url="localhost:20128"))

    assert report.exit_code == 2 and "http://" in format_report(report)


def test_the_script_parses_repeated_models() -> None:
    args = check_llm.parse_args(["--model", "a", "--model", "b", "--embedding-model", "hash"])

    assert args.model == ["a", "b"] and args.embedding_model == "hash"


def test_the_script_prints_the_report_and_returns_its_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = FakeGateway(api_key=KEY)
    real_run_checks = check_llm.run_checks

    async def through_the_fake(config: Settings, **options: object) -> CheckReport:
        return await real_run_checks(config, http_client=gateway.client(), **options)  # type: ignore[arg-type]

    monkeypatch.setattr(check_llm, "get_settings", gateway_settings)
    monkeypatch.setattr(check_llm, "run_checks", through_the_fake)

    assert check_llm.main(["--model", "auto/fast"]) == 0

    output = capsys.readouterr().out
    assert "Everything passed." in output and KEY not in output


async def test_listing_models_sorts_and_filters_by_text() -> None:
    gateway = FakeGateway(api_key=KEY)

    some, problem = await list_model_ids(
        gateway_settings(), contains="EMBED", http_client=gateway.client()
    )
    every, _ = await list_model_ids(gateway_settings(), http_client=gateway.client())

    assert (some, problem) == (["fake/embed-small"], "")
    assert every == sorted(gateway.models)


async def test_listing_models_explains_what_is_wrong() -> None:
    rejected, problem = await list_model_ids(
        gateway_settings(), http_client=FakeGateway(api_key="another-key").client()
    )
    assert not rejected and "rejected the API key" in problem

    assert "OPENAI_API_KEY" in (await list_model_ids(settings()))[1]
    assert "http://" in (await list_model_ids(settings(llm_base_url="oops")))[1]


def test_the_script_lists_models(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = FakeGateway(api_key=KEY)
    real_list = check_llm.list_model_ids

    async def through_the_fake(config: Settings, **options: object) -> tuple[list[str], str]:
        return await real_list(config, http_client=gateway.client(), **options)  # type: ignore[arg-type]

    monkeypatch.setattr(check_llm, "get_settings", gateway_settings)
    monkeypatch.setattr(check_llm, "list_model_ids", through_the_fake)

    assert check_llm.main(["--list", "embed"]) == 0
    assert capsys.readouterr().out.split() == ["fake/embed-small"]
    assert check_llm.main(["--list"]) == 0
    assert capsys.readouterr().out.split() == sorted(gateway.models)
    assert check_llm.main(["--list", "zzz"]) == 0
    assert "No model matches" in capsys.readouterr().out
