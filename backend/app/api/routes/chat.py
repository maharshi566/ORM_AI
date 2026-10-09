"""POST /api/chat: ask ORM_AI a question about one shop.

Try it at http://localhost:8000/docs ("POST /api/chat", "Try it out"):

    {"shop_id": "SHOP-001", "message": "How much does CUST-0001 owe?"}

The reply includes ``session_id``; send it back with the next message to continue the
same conversation. Each request runs the whole agent graph (see app/graph/workflow.py)
and returns the answer with its sources, proposed actions, tool calls and agent steps.

``GET /api/chat/graph`` returns the agent graph as a Mermaid diagram.
"""

from fastapi import APIRouter, Request

from app.graph.workflow import draw_mermaid
from app.models.schemas import ChatRequest, ChatResponse, GraphResponse
from app.services.chat_service import ChatService, get_agent_runtime

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse, summary="Ask ORM_AI about a shop")
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    runtime = await get_agent_runtime(request.app)
    return await ChatService(runtime).handle(body)


@router.get("/graph", response_model=GraphResponse, summary="The agent graph as Mermaid")
async def graph() -> GraphResponse:
    return GraphResponse(mermaid=draw_mermaid())
