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

When logged in (Phase 6), leave ``user_id`` out: the token says who decides.

The reply has the same shape as ``POST /api/chat``: what was done (only what a tool
confirmed), what was not and why. ``GET`` shows what is waiting and what was decided.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from app.api.identity import required_user
from app.core.auth import CurrentUser
from app.core.rate_limit import rate_limit
from app.models.schemas import ApprovalDecisionRequest, ApprovalStatusResponse, ChatResponse
from app.services.approval_service import ApprovalService
from app.services.chat_service import get_agent_runtime

router = APIRouter(prefix="/approval", tags=["approval"])

WorkflowId = Annotated[str, Path(max_length=36, pattern=r"^[A-Za-z0-9-]+$")]


@router.post(
    "/{workflow_id}",
    response_model=ChatResponse,
    summary="Approve, change or reject the waiting actions, then resume",
)
async def decide(
    request: Request,
    body: ApprovalDecisionRequest,
    principal: CurrentUser,
    _: Annotated[None, Depends(rate_limit("agent"))],
    workflow_id: WorkflowId,
) -> ChatResponse:
    user_id = required_user(principal, body.user_id)
    runtime = await get_agent_runtime(request.app)
    return await ApprovalService(runtime).decide(
        workflow_id, body.model_copy(update={"user_id": user_id}), principal=principal
    )


@router.get(
    "/{workflow_id}",
    response_model=ApprovalStatusResponse,
    summary="What is waiting for approval, and what was decided",
)
async def status(
    request: Request, principal: CurrentUser, workflow_id: WorkflowId
) -> ApprovalStatusResponse:
    runtime = await get_agent_runtime(request.app)
    return await ApprovalService(runtime).status(workflow_id, principal=principal)
