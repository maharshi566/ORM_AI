"""The agents' model client: structured output, repairs, fallback model, tool calls.

The fake gateway (tests/fake_gateway.py) is reached through the real OpenAI SDK, so
these tests check the exact requests the app sends and how it reads the answers.
"""

import json
from typing import Literal

import pytest
from pydantic import BaseModel

from app.config.settings import Settings
from app.services.chat_model import (
    LLMError,
    OpenAIChatModel,
    ToolCallRequest,
    ToolRoundReply,
    _raw_assistant_message,
    chat_model_problem,
)
from app.tools.registry import AGENT_TOOLS, ToolRegistry
from tests.fake_gateway import BASE_URL, SIGNATURE, FakeGateway

KEY = "sk-gateway-key-123"


class Verdict(BaseModel):
    intent: Literal["stock", "credit"]
    confidence: float
    notes: list[str]


GOOD = json.dumps({"intent": "credit", "confidence": 0.9, "notes": ["owes Rs 3,900"]})
USER = [{"role": "user", "content": "How much does Ramesh owe?"}]


def settings(**overrides) -> Settings:
    values = {
        "llm_base_url": BASE_URL,
        "llm_api_key": KEY,
        "llm_model_fast": "auto/fast",
        "llm_model_smart": "auto/smart",
        "llm_max_retries": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def model(gateway: FakeGateway, **overrides) -> OpenAIChatModel:
    return OpenAIChatModel(settings(**overrides), http_client=gateway.client())


def test_problems_are_explained_before_any_call() -> None:
    nothing = Settings(_env_file=None)
    no_model = Settings(_env_file=None, openai_api_key="sk-x")

    assert "OPENAI_API_KEY" in chat_model_problem(nothing)
    assert "LLM_MODEL_FAST" in chat_model_problem(no_model)
    assert chat_model_problem(settings()) is None
    with pytest.raises(LLMError) as raised:
        OpenAIChatModel(no_model)
    assert raised.value.kind == "not_configured"


def test_one_model_name_serves_both_tiers() -> None:
    llm = OpenAIChatModel(settings(llm_model_smart=""))

    assert llm.model_for("fast") == llm.model_for("smart") == "auto/fast"


async def test_structured_output_uses_a_strict_json_schema() -> None:
    gateway = FakeGateway(api_key=KEY, replies=[GOOD])

    reply = await model(gateway).structured(
        Verdict, system="Classify.", messages=USER, tier="smart"
    )

    [request] = gateway.sent_to("/v1/chat/completions")
    response_format = request.body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False
    assert request.body["model"] == "auto/smart"
    assert request.body["messages"][0] == {"role": "system", "content": "Classify."}
    assert reply.value == Verdict(intent="credit", confidence=0.9, notes=["owes Rs 3,900"])
    assert reply.mode == "json_schema"
    assert (reply.usage.input_tokens, reply.usage.output_tokens, reply.usage.calls) == (5, 2, 1)


async def test_models_without_json_schema_get_json_mode_and_the_schema_in_the_prompt() -> None:
    gateway = FakeGateway(api_key=KEY, json_schema=False, replies=[GOOD, GOOD])
    llm = model(gateway)

    first = await llm.structured(Verdict, system="Classify.", messages=USER, tier="fast")
    second = await llm.structured(Verdict, system="Classify.", messages=USER, tier="fast")

    formats = [r.body["response_format"]["type"] for r in gateway.sent_to("/v1/chat/completions")]
    assert formats == ["json_schema", "json_object", "json_object"]  # remembered per model
    assert first.mode == second.mode == "json_object"
    system = gateway.sent_to("/v1/chat/completions")[1].body["messages"][0]["content"]
    assert "JSON schema" in system and '"intent"' in system


async def test_a_reply_that_breaks_the_schema_is_repaired_once() -> None:
    wrong = json.dumps({"intent": "weather", "confidence": 0.5, "notes": []})
    gateway = FakeGateway(api_key=KEY, replies=[wrong, GOOD])

    reply = await model(gateway).structured(Verdict, system="Classify.", messages=USER, tier="fast")

    second = gateway.sent_to("/v1/chat/completions")[1].body["messages"]
    assert second[-2] == {"role": "assistant", "content": wrong}
    assert "intent" in second[-1]["content"] and "did not match" in second[-1]["content"]
    assert reply.value.intent == "credit"
    assert (reply.usage.calls, reply.usage.input_tokens) == (2, 10)


async def test_a_model_that_never_matches_the_schema_fails_clearly() -> None:
    gateway = FakeGateway(api_key=KEY, replies=["not json", '{"intent": "stock"}'])

    with pytest.raises(LLMError) as raised:
        await model(gateway).structured(Verdict, system="Classify.", messages=USER, tier="fast")

    assert raised.value.kind == "malformed_output"
    assert "Verdict" in raised.value.message and "2 tries" in raised.value.message


async def test_markdown_fences_around_json_are_accepted() -> None:
    gateway = FakeGateway(api_key=KEY, json_schema=False, replies=[f"```json\n{GOOD}\n```"])

    reply = await model(gateway).structured(Verdict, system="Classify.", messages=USER, tier="fast")

    assert reply.value.intent == "credit"


async def test_json_object_setting_never_sends_a_schema_format() -> None:
    gateway = FakeGateway(api_key=KEY, replies=[GOOD])

    await model(gateway, llm_structured_output="json_object").structured(
        Verdict, system="Classify.", messages=USER, tier="fast"
    )

    [request] = gateway.sent_to("/v1/chat/completions")
    assert request.body["response_format"] == {"type": "json_object"}


async def test_the_fallback_model_answers_when_the_main_one_is_missing() -> None:
    gateway = FakeGateway(api_key=KEY, replies=[GOOD])

    reply = await model(
        gateway, llm_model_fast="retired-model", llm_model_fallback="auto"
    ).structured(Verdict, system="Classify.", messages=USER, tier="fast")

    models = [r.body["model"] for r in gateway.sent_to("/v1/chat/completions")]
    assert models == ["retired-model", "auto"]
    assert reply.usage.fallback_used is True and reply.usage.model == "auto"


async def test_a_rejected_key_is_not_retried_on_the_fallback() -> None:
    gateway = FakeGateway(api_key="another-key")

    with pytest.raises(LLMError) as raised:
        await model(gateway, llm_model_fallback="auto").structured(
            Verdict, system="Classify.", messages=USER, tier="fast"
        )

    assert len(gateway.sent_to("/v1/chat/completions")) == 1
    assert raised.value.kind == "unavailable"
    assert "LLM_API_KEY" in raised.value.message
    assert KEY not in raised.value.message


async def test_tool_round_returns_the_requested_calls() -> None:
    gateway = FakeGateway(api_key=KEY)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_stock",
                "description": "Stock for one SKU",
                "parameters": {"type": "object", "properties": {"sku": {"type": "string"}}},
            },
        }
    ]

    reply = await model(gateway).tool_round(
        system="Use tools.", messages=USER, tools=tools, tier="fast"
    )

    [request] = gateway.sent_to("/v1/chat/completions")
    assert request.body["tools"] == tools and request.body["tool_choice"] == "auto"
    [call] = reply.tool_calls
    assert (call.name, json.loads(call.arguments)) == ("get_stock", {"sku": "KIR-001"})
    assert reply.assistant_message()["tool_calls"][0]["function"]["name"] == "get_stock"


async def test_reasoning_effort_is_sent_only_when_set() -> None:
    plain, tuned = (
        FakeGateway(api_key=KEY, replies=[GOOD]),
        FakeGateway(api_key=KEY, replies=[GOOD]),
    )

    await model(plain).structured(Verdict, system="x", messages=USER, tier="fast")
    await model(tuned, llm_reasoning_effort="low").structured(
        Verdict, system="x", messages=USER, tier="fast"
    )

    assert "reasoning_effort" not in plain.sent_to("/v1/chat/completions")[0].body
    assert tuned.sent_to("/v1/chat/completions")[0].body["reasoning_effort"] == "low"


# ------------------------------------------------------------ Gemini and other providers

STOCK_TOOL = {
    "type": "function",
    "function": {
        "name": "get_stock",
        "description": "Stock for one SKU",
        "parameters": {"type": "object", "properties": {"sku": {"type": "string"}}},
    },
}


async def test_gemini_thought_signatures_go_back_with_the_tool_results() -> None:
    """Gemini 3 refuses the second round (HTTP 400) if the signature is dropped."""
    gateway = FakeGateway(api_key=KEY, gemini=True)
    llm = model(gateway)

    first = await llm.tool_round(
        system="Use tools.", messages=USER, tools=[STOCK_TOOL], tier="fast"
    )
    [call] = first.tool_calls
    history = [
        *USER,
        first.assistant_message(),
        {"role": "tool", "tool_call_id": call.id, "content": '{"on_hand": 4}'},
    ]
    second = await llm.tool_round(
        system="Use tools.", messages=history, tools=[STOCK_TOOL], tier="fast"
    )

    sent = gateway.sent_to("/v1/chat/completions")[1].body["messages"]
    assert sent[2]["tool_calls"][0]["extra_content"] == SIGNATURE
    assert sent[2]["tool_calls"][0]["id"] == sent[3]["tool_call_id"] == "call_1"
    assert (second.content, second.tool_calls) == ("done", [])


async def test_dropping_the_signature_is_what_gemini_refuses() -> None:
    """The check above is real: the fake refuses a turn rebuilt without the signature."""
    gateway = FakeGateway(api_key=KEY, gemini=True)
    llm = model(gateway)
    first = await llm.tool_round(system="x", messages=USER, tools=[STOCK_TOOL], tier="fast")
    rebuilt = ToolRoundReply(content=first.content, tool_calls=first.tool_calls)

    with pytest.raises(LLMError) as raised:
        await llm.tool_round(
            system="x",
            messages=[
                *USER,
                rebuilt.assistant_message(),
                {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
            ],
            tools=[STOCK_TOOL],
            tier="fast",
        )

    assert raised.value.kind == "rejected"


async def test_every_tool_schema_is_sent_in_a_form_gemini_accepts() -> None:
    gateway = FakeGateway(api_key=KEY, gemini=True)
    registry = ToolRegistry()
    tools = [schema for agent in AGENT_TOOLS for schema in registry.schemas_for(agent)]

    reply = await model(gateway).tool_round(system="x", messages=USER, tools=tools, tier="fast")

    [request] = gateway.sent_to("/v1/chat/completions")
    sent = json.dumps(request.body["tools"])
    assert reply.tool_calls  # accepted, not refused with 400
    assert "additionalProperties" not in sent and "$ref" not in sent
    assert [t["function"]["name"] for t in request.body["tools"]] == [
        t["function"]["name"] for t in tools
    ]


async def test_a_schema_refusal_in_other_words_still_falls_back_to_json_mode() -> None:
    gemini_words = (
        'Invalid JSON payload received. Unknown name "additionalProperties" at '
        "'generation_config.response_schema': Cannot find field."
    )
    gateway = FakeGateway(api_key=KEY, json_schema=False, schema_error=gemini_words, replies=[GOOD])

    reply = await model(gateway).structured(Verdict, system="x", messages=USER, tier="fast")

    formats = [r.body["response_format"]["type"] for r in gateway.sent_to("/v1/chat/completions")]
    assert formats == ["json_schema", "json_object"]
    assert reply.mode == "json_object" and reply.value.intent == "credit"


async def test_a_request_refused_in_both_modes_is_not_blamed_on_the_format() -> None:
    gateway = FakeGateway(api_key=KEY, json_schema=False, json_mode=False)
    llm = model(gateway)

    for _ in range(2):
        with pytest.raises(LLMError) as raised:
            await llm.structured(Verdict, system="x", messages=USER, tier="fast")
        assert raised.value.kind == "rejected"

    formats = [r.body["response_format"]["type"] for r in gateway.sent_to("/v1/chat/completions")]
    assert formats == ["json_schema", "json_object"] * 2  # strict schemas tried again


def test_output_only_fields_are_not_sent_back() -> None:
    call = ToolCallRequest(id="call_9", name="get_stock", arguments="{}")
    message = {
        "role": "assistant",
        "content": None,
        "refusal": None,
        "annotations": [],
        "audio": {"id": "a1"},
        "reasoning_details": [{"type": "reasoning.encrypted", "data": "xyz"}],
        "tool_calls": [{"id": "", "type": "function", "function": {"name": "get_stock"}}],
    }

    raw = _raw_assistant_message(message, [(call, message["tool_calls"][0])])

    assert set(raw) == {"role", "content", "tool_calls", "reasoning_details"}
    assert raw["tool_calls"] == [
        {"id": "call_9", "type": "function", "function": {"name": "get_stock", "arguments": "{}"}}
    ]
