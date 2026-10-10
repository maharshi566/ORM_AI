"""What the agents did: conversations, workflows and metrics (read only).

* ``GET /api/sessions/{session_id}``: a conversation's messages and its workflows.
* ``GET /api/workflows/{workflow_id}``: one workflow: status, every agent step (model,
  time, tokens, summary), every tool call, the approvals, and what is waiting.
* ``GET /api/metrics``: counts and timings for the admin page: workflows by status,
  each agent's runs, errors, average and 95th-percentile time and tokens, each tool's
  calls and error codes, approvals by status, documents and chunks.

A logged-in shop user sees only their shop; an admin, or local development without a
token, sees every shop.
"""

from typing import Annotated

from fastapi import APIRouter, Path, Request

from app.api.identity import session_factory_for
from app.core.auth import CurrentUser
from app.models.schemas import (
    ApprovalRequestView,
    MetricsResponse,
    SessionResponse,
    WorkflowResponse,
)
from app.services import records_service
from app.services.approval_service import ApprovalService
from app.services.chat_service import get_agent_runtime

router = APIRouter(tags=["records"])
SessionId = Annotated[str, Path(max_length=36, pattern=r"^[A-Za-z0-9-]+$")]
WorkflowId = Annotated[str, Path(max_length=36, pattern=r"^[A-Za-z0-9-]+$")]


@router.get("/sessions/{session_id}", response_model=SessionResponse, summary="A conversation")
async def session(
    request: Request, principal: CurrentUser, session_id: SessionId
) -> SessionResponse:
    return await records_service.session_view(session_factory_for(request), session_id, principal)


@router.get(
    "/workflows/{workflow_id}",
    response_model=WorkflowResponse,
    summary="One workflow: steps, tool calls and approvals",
)
async def workflow(
    request: Request, principal: CurrentUser, workflow_id: WorkflowId
) -> WorkflowResponse:
    view, row = await records_service.workflow_view(
        session_factory_for(request), workflow_id, principal
    )
    if str(row.status) == "awaiting_approval":
        runtime = await get_agent_runtime(request.app)
        waiting = await ApprovalService(runtime).waiting_request(row)
        if waiting:
            view.approval = ApprovalRequestView.model_validate(waiting)
    return view


@router.get("/metrics", response_model=MetricsResponse, summary="Counts and timings")
async def metrics(request: Request, principal: CurrentUser) -> MetricsResponse:
    return await records_service.metrics_view(session_factory_for(request), principal)
