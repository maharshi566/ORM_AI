"""A small fake of an OpenAI-compatible gateway (the kind OmniRoute is), for tests.

It answers just enough of the API: ``GET /v1/models``, ``POST /v1/chat/completions``
(plain, JSON mode, strict JSON schema and tool calls) and ``POST /v1/embeddings``.
Tests reach it through an in-process HTTP client, so no network, port or real service
is involved, and every request is recorded in ``gateway.requests`` so a test can check
what the app sent.
"""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

BASE_URL = "http://gateway.local/v1"  # ".local" counts as a private address


@dataclass
class Recorded:
    method: str
    path: str
    authorization: str
    body: dict[str, Any]


def _error(status: int, message: str, code: str = "error") -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error", "code": code}},
        status_code=status,
    )


@dataclass
class FakeGateway:
    """Behaviour is switched with the fields, for example ``FakeGateway(json_mode=False)``."""

    api_key: str | None = None  # when set, calls without "Bearer <key>" get 401
    models: tuple[str, ...] = ("auto", "auto/fast", "auto/smart", "fake/embed-small")
    embedding_models: tuple[str, ...] = ("fake/embed-small",)
    json_mode: bool = True  # False: JSON mode is rejected with 400
    json_schema: bool = True  # False: strict JSON-schema output is rejected with 400
    replies: list[str] = field(default_factory=list)  # JSON-mode/schema answers, in order
    tool_calls: bool = True  # False: tools are ignored and the model answers in text
    dimensions: int = 8
    fail: dict[str, int] = field(default_factory=dict)  # path -> status to answer with
    requests: list[Recorded] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.app = Starlette(
            routes=[
                Route("/v1/models", self._models, methods=["GET"]),
                Route("/v1/chat/completions", self._chat, methods=["POST"]),
                Route("/v1/embeddings", self._embeddings, methods=["POST"]),
            ]
        )

    def client(self) -> httpx2.AsyncClient:
        """An HTTP client wired straight to this fake (pass it as ``http_client``)."""
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), base_url="http://gateway.local"
        )

    def sent_to(self, path: str) -> list[Recorded]:
        return [request for request in self.requests if request.path == path]

    # ------------------------------------------------------------ plumbing

    async def _gate(self, request: Request) -> tuple[dict[str, Any], JSONResponse | None]:
        body = json.loads(await request.body() or b"{}")
        authorization = request.headers.get("authorization", "")
        self.requests.append(Recorded(request.method, request.url.path, authorization, body))
        if self.api_key is not None and authorization != f"Bearer {self.api_key}":
            return body, _error(401, "Invalid API key.", "invalid_api_key")
        if request.url.path in self.fail:
            status = self.fail[request.url.path]
            return body, _error(status, f"Upstream provider failed with {status}.")
        return body, None

    # ------------------------------------------------------------ endpoints

    async def _models(self, request: Request) -> JSONResponse:
        _, problem = await self._gate(request)
        if problem:
            return problem
        data = [
            {"id": name, "object": "model", "created": 0, "owned_by": "fake"}
            for name in self.models
        ]
        return JSONResponse({"object": "list", "data": data})

    async def _chat(self, request: Request) -> JSONResponse:
        body, problem = await self._gate(request)
        if problem:
            return problem
        model = body.get("model", "")
        if model not in self.models:
            return _error(404, f"Model '{model}' not found.", "model_not_found")
        message: dict[str, Any] = {"role": "assistant", "content": "ready"}
        finish = "stop"
        messages = body.get("messages", [])
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

        if body.get("tools") and self.tool_calls:
            call = {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_stock", "arguments": json.dumps({"sku": "KIR-001"})},
            }
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
            finish = "tool_calls"
        elif (body.get("response_format") or {}).get("type") == "json_schema":
            if not self.json_schema:
                return _error(
                    400,
                    "Invalid parameter: 'response_format' of type 'json_schema' is not "
                    "supported with this model.",
                )
            message["content"] = self.replies.pop(0) if self.replies else '{"status": "ok"}'
        elif (body.get("response_format") or {}).get("type") == "json_object":
            if not self.json_mode:
                return _error(400, "response_format json_object is not supported.")
            message["content"] = (
                self.replies.pop(0) if self.replies else self._json_reply(last_user)
            )

        return JSONResponse(
            {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            }
        )

    @staticmethod
    def _json_reply(last_user: str) -> str:
        """A rerank request (it has "passages") gets the last passage scored best."""
        try:
            passages = json.loads(last_user).get("passages", [])
        except (ValueError, AttributeError):
            passages = []
        if not passages:
            return json.dumps({"status": "ok"})
        scores = [{"id": item["id"], "score": index} for index, item in enumerate(passages)]
        return json.dumps({"scores": scores})

    async def _embeddings(self, request: Request) -> JSONResponse:
        body, problem = await self._gate(request)
        if problem:
            return problem
        model = body.get("model", "")
        if model not in self.embedding_models:
            return _error(404, f"No provider offers the embedding model '{model}'.")
        texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
        data = [
            {
                "object": "embedding",
                "index": index,
                "embedding": [float(len(text)), float(index)] + [0.0] * (self.dimensions - 2),
            }
            for index, text in enumerate(texts)
        ]
        return JSONResponse(
            {
                "object": "list",
                "data": data,
                "model": model,
                "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
            }
        )


def unreachable_client() -> httpx2.AsyncClient:
    """A client whose every request fails to connect, like a gateway that is not running."""

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(refuse))
