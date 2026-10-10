"""POST /api/agent/run: run the agents on one task, without a conversation.

The same agent graph as ``POST /api/chat``, for callers that are not a chat: a
scheduled check, another system, a button in the frontend ("Check late deliveries").
Nothing is remembered between runs, so there is no ``session_id``. A task that needs
approval pauses the same way, and ``POST /api/approval/{workflow_id}`` resumes it.

    {"shop_id": "SHOP-002", "task": "PO-00585 is 7 days late. Message the supplier."}
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.identity import acting_user
from app.core.auth import CurrentUser
from app.core.rate_limit import rate_limit
from app.models.schemas import AgentRunRequest, ChatRequest, ChatResponse
from app.services.chat_service import ChatService, get_agent_runtime

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/run", response_model=ChatResponse, summary="Run the agents on one task")
async def run(
    request: Request,
    body: AgentRunRequest,
    principal: CurrentUser,
    _: Annotated[None, Depends(rate_limit("agent"))],
) -> ChatResponse:
    user_id = acting_user(principal, body.shop_id, body.user_id)
    runtime = await get_agent_runtime(request.app)
    chat = ChatRequest(shop_id=body.shop_id, message=body.task, user_id=user_id)
    return await ChatService(runtime).handle(chat, remember=False)
