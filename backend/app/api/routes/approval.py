"""Approve, change or reject what ORM_AI proposed, then let it carry on.

When a chat reply has ``"status": "awaiting_approval"``, its ``approval`` block lists
each waiting action with an ``approval_id``, who must decide (staff or owner), why,
and the evidence and rules behind it. Nothing has been changed yet.

Try it at http://localhost:8000/docs ("POST /api/approval/{workflow_id}"):

    {"user_id": "USR-003", "decision": "approve"}                 # everything
    {"user_id": "USR-003", "decision": "reject", "note": "Call them first"}
    {"user_id": "USR-007", "decisions": [
        {"approval_id": "...", "decision": "modify",
         "arguments": {"product_id": "PRD-0085", "new_selling_price": 358,
                       "reason": "Selling below cost; owner set Rs 358."}}]}

The reply has the same shape as ``POST /api/chat``: what was done (only what a tool
confirmed), what was not and why. ``GET`` shows what is waiting and what was decided.
"""

from fastapi import APIRouter, Path, Request

from app.models.schemas import ApprovalDecisionRequest, ApprovalStatusResponse, ChatResponse
from app.services.approval_service import ApprovalService
from app.services.chat_service import get_agent_runtime

router = APIRouter(prefix="/approval", tags=["approval"])

WorkflowId = Path(max_length=36, pattern=r"^[A-Za-z0-9-]+$")


@router.post(
    "/{workflow_id}",
    response_model=ChatResponse,
    summary="Approve, change or reject the waiting actions, then resume",
)
async def decide(
    request: Request, body: ApprovalDecisionRequest, workflow_id: str = WorkflowId
) -> ChatResponse:
    runtime = await get_agent_runtime(request.app)
    return await ApprovalService(runtime).decide(workflow_id, body)


@router.get(
    "/{workflow_id}",
    response_model=ApprovalStatusResponse,
    summary="What is waiting for approval, and what was decided",
)
async def status(request: Request, workflow_id: str = WorkflowId) -> ApprovalStatusResponse:
    runtime = await get_agent_runtime(request.app)
    return await ApprovalService(runtime).status(workflow_id)
