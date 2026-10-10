"""POST /api/chat: ask ORM_AI a question about one shop.

Try it at http://localhost:8000/docs ("POST /api/chat", "Try it out"):

    {"shop_id": "SHOP-001", "message": "How much does CUST-0001 owe?"}

The reply includes ``session_id``; send it back with the next message to continue the
same conversation. Each request runs the whole agent graph (see app/graph/workflow.py)
and returns the answer with its sources, proposed actions, tool calls and agent steps.

``POST /api/chat/stream`` does the same, but sends progress as it happens, as
Server-Sent Events: one ``stage`` event when each agent starts and finishes (triage,
retrieval, investigation, approval, action, response, validation), an
``approval_requested`` event when the workflow pauses for a person, and finally one
``result`` event with the same JSON as ``POST /api/chat`` (or one ``error`` event).

``GET /api/chat/graph`` returns the agent graph as a Mermaid diagram.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.identity import acting_user
from app.core.auth import CurrentUser
from app.core.exceptions import AppError, describe_error
from app.core.logging import get_logger
from app.core.rate_limit import rate_limit
from app.graph.workflow import draw_mermaid
from app.models.schemas import ChatRequest, ChatResponse, GraphResponse
from app.services.chat_service import ChatService, get_agent_runtime

logger = get_logger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])
AgentLimit = Annotated[None, Depends(rate_limit("agent"))]

# The stage the frontend shows for each node of the graph.
STAGES = {
    "triage": "triage",
    "supervisor": "routing",
    "data_retrieval": "retrieval",
    "knowledge": "retrieval",
    "investigation": "investigation",
    "human_review": "approval",
    "action": "action",
    "respond": "response",
    "clarify": "response",
    "validate": "validation",
    "finalize": "response",
}


def with_identity(body: ChatRequest, principal: Any) -> ChatRequest:
    user_id = acting_user(principal, body.shop_id, body.user_id)
    return body if user_id == body.user_id else body.model_copy(update={"user_id": user_id})


@router.post("", response_model=ChatResponse, summary="Ask ORM_AI about a shop")
async def chat(
    request: Request, body: ChatRequest, principal: CurrentUser, _: AgentLimit
) -> ChatResponse:
    runtime = await get_agent_runtime(request.app)
    return await ChatService(runtime).handle(with_identity(body, principal))


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"


def stage_event(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    kind = event.get("type", "")
    if kind == "approval_requested":
        return "approval_requested", event
    if kind in {"agent_started", "agent_finished"}:
        data = {
            "stage": STAGES.get(event.get("agent", ""), event.get("agent")),
            "agent": event.get("agent"),
            "state": "started" if kind == "agent_started" else "finished",
        }
        for key in ("status", "summary", "latency_ms", "model", "error"):
            if key in event:
                data[key] = event[key]
        return "stage", data
    return "progress", event


@router.post(
    "/stream",
    summary="Ask ORM_AI and follow the agents' progress (Server-Sent Events)",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def chat_stream(
    request: Request, body: ChatRequest, principal: CurrentUser, _: AgentLimit
) -> StreamingResponse:
    runtime = await get_agent_runtime(request.app)
    body = with_identity(body, principal)
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def on_event(event: dict[str, Any]) -> None:
        name, data = stage_event(event)
        await queue.put(sse(name, data))

    async def work() -> None:
        try:
            response = await ChatService(runtime).handle(body, on_event=on_event)
            await queue.put(sse("result", response.model_dump(mode="json")))
        except Exception as exc:
            # The same status, code and message POST /api/chat would answer with.
            status, code, message = describe_error(exc)
            if status >= 500 and not isinstance(exc, AppError):
                logger.exception("chat_stream_failed", error_type=type(exc).__name__)
            await queue.put(sse("error", {"code": code, "message": message, "status": status}))
        finally:
            await queue.put(None)

    # The workflow runs to the end even if the browser goes away: stopping halfway
    # could leave an approved action half reported.
    task = asyncio.create_task(work())
    request.app.state.stream_tasks.add(task)  # finished before shutdown (app/main.py)
    task.add_done_callback(request.app.state.stream_tasks.discard)

    async def events() -> AsyncIterator[str]:
        while True:
            item = await queue.get()
            if item is None:
                return
            yield item

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/graph", response_model=GraphResponse, summary="The agent graph as Mermaid")
async def graph() -> GraphResponse:
    return GraphResponse(mermaid=draw_mermaid())
