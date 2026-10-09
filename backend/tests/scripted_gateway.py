"""An OpenAI-compatible endpoint whose "model" is the scripted RuleBasedLLM.

The agent graph can then run through the real client (``OpenAIChatModel`` and the
OpenAI SDK) with no network: every request is real JSON over an in-process HTTP
transport. The gateway also checks each structured-output request the way OpenAI's
strict mode does (every object closes ``additionalProperties`` and requires all of its
properties), so a schema OpenAI would reject fails here first.
"""

import json
from typing import Any

import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.agents.schemas import FinalResponse, InvestigationResult, TriageResult
from tests.fake_llm import RuleBasedLLM

SCHEMAS = {model.__name__: model for model in (TriageResult, InvestigationResult, FinalResponse)}


def strict_problems(schema: dict[str, Any], where: str = "$") -> list[str]:
    """What OpenAI's strict mode would reject in this JSON schema."""
    problems: list[str] = []
    if schema.get("type") == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is not False:
            problems.append(f"{where}: additionalProperties must be false")
        if sorted(schema.get("required", [])) != sorted(properties):
            problems.append(f"{where}: every property must be required")
        for name, sub in properties.items():
            problems += strict_problems(sub, f"{where}.{name}")
    for key in ("anyOf", "allOf", "oneOf"):
        for index, sub in enumerate(schema.get(key, [])):
            problems += strict_problems(sub, f"{where}.{key}[{index}]")
    if "items" in schema:
        problems += strict_problems(schema["items"], f"{where}[]")
    for name, sub in schema.get("$defs", {}).items():
        problems += strict_problems(sub, f"$defs.{name}")
    return problems


class ScriptedGateway:
    def __init__(self, llm: RuleBasedLLM | None = None) -> None:
        self.llm = llm or RuleBasedLLM()
        self.requests: list[dict[str, Any]] = []
        self.app = Starlette(routes=[Route("/v1/chat/completions", self._chat, methods=["POST"])])

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), base_url="http://scripted.local"
        )

    async def _chat(self, request: Request) -> JSONResponse:
        body = json.loads(await request.body())
        self.requests.append(body)
        system = body["messages"][0]["content"]
        messages = body["messages"][1:]
        message: dict[str, Any]
        if body.get("tools"):
            reply = await self.llm.tool_round(
                system=system, messages=messages, tools=body["tools"], tier="fast"
            )
            message = reply.assistant_message()
            finish = "tool_calls" if reply.tool_calls else "stop"
        else:
            spec = body["response_format"]["json_schema"]
            problems = strict_problems(spec["schema"])
            if problems:
                error = {"message": "Invalid schema: " + "; ".join(problems[:3])}
                return JSONResponse({"error": error}, status_code=400)
            reply = await self.llm.structured(
                SCHEMAS[spec["name"]], system=system, messages=messages, tier="smart"
            )
            message = {"role": "assistant", "content": reply.value.model_dump_json()}
            finish = "stop"
        return JSONResponse(
            {
                "id": "chatcmpl-scripted",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
            }
        )
