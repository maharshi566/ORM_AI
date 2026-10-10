"""What the agents did: conversations, workflows and metrics (read only).

* ``GET /api/sessions/{session_id}``: a conversation's messages and its workflows.
* ``GET /api/workflows/{workflow_id}``: one workflow: status, every agent step (model,
  time, tokens, summary), every tool call, the approvals, what is waiting, and the
  reply as the chat gave it (``reply``, rebuilt from the saved graph state).
* ``GET /api/metrics``: counts and timings for the admin page: workflows by status,
  each agent's runs, errors, average and 95th-percentile time and tokens, each tool's
  calls and error codes, approvals by status, documents and chunks.

Lists for the frontend (Phase 7):

* ``GET /api/shops``: the shops you can ask about (yours, or all for an admin).
* ``GET /api/sessions``: your recent conversations (the chat page's list).
* ``GET /api/workflows``: recent requests, optionally by status (the admin page).
* ``GET /api/approvals``: the approvals inbox, optionally by status.
* ``GET /api/evaluations``: the latest evaluation runs, case by case (admins only).

A logged-in shop user sees only their shop; an admin, or local development without a
token, sees every shop (or the one named with ``shop_id``).
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request

from app.api.identity import session_factory_for
from app.core.auth import CurrentUser
from app.models.schemas import (
    ApprovalListResponse,
    ApprovalRequestView,
    EvaluationsResponse,
    MetricsResponse,
    SessionListResponse,
    SessionResponse,
    ShopListResponse,
    WorkflowListResponse,
    WorkflowResponse,
)
from app.services import records_service
from app.services.approval_service import ApprovalService
from app.services.chat_service import get_agent_runtime, stored_reply

router = APIRouter(tags=["records"])
SessionId = Annotated[str, Path(max_length=36, pattern=r"^[A-Za-z0-9-]+$")]
WorkflowId = Annotated[str, Path(max_length=36, pattern=r"^[A-Za-z0-9-]+$")]
ShopFilter = Annotated[
    str | None,
    Query(pattern=r"^SHOP-\d{3}$", description="One shop (admins); shop users get their own"),
]
WorkflowStatusFilter = Annotated[
    Literal["running", "awaiting_approval", "completed", "failed", "blocked"] | None, Query()
]
ApprovalStatusFilter = Annotated[
    Literal["pending", "approved", "modified", "rejected"] | None, Query()
]


@router.get("/shops", response_model=ShopListResponse, summary="The shops you can ask about")
async def shops(request: Request, principal: CurrentUser) -> ShopListResponse:
    return await records_service.list_shops(session_factory_for(request), principal)


@router.get("/sessions", response_model=SessionListResponse, summary="Your recent conversations")
async def sessions(
    request: Request,
    principal: CurrentUser,
    shop_id: ShopFilter = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SessionListResponse:
    return await records_service.list_sessions(
        session_factory_for(request), principal, shop_id=shop_id, limit=limit
    )


@router.get("/workflows", response_model=WorkflowListResponse, summary="Recent requests")
async def workflows(
    request: Request,
    principal: CurrentUser,
    shop_id: ShopFilter = None,
    status: WorkflowStatusFilter = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> WorkflowListResponse:
    return await records_service.list_workflows(
        session_factory_for(request), principal, shop_id=shop_id, status=status, limit=limit
    )


@router.get(
    "/approvals",
    response_model=ApprovalListResponse,
    summary="The approvals inbox: what waits for a person, and what was decided",
)
async def approvals(
    request: Request,
    principal: CurrentUser,
    shop_id: ShopFilter = None,
    status: ApprovalStatusFilter = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ApprovalListResponse:
    return await records_service.list_approvals(
        session_factory_for(request), principal, shop_id=shop_id, status=status, limit=limit
    )


@router.get(
    "/evaluations",
    response_model=EvaluationsResponse,
    summary="The latest evaluation runs (admins)",
)
async def evaluations(
    request: Request,
    principal: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> EvaluationsResponse:
    return await records_service.list_evaluations(
        session_factory_for(request), principal, limit=limit
    )


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
    runtime = await get_agent_runtime(request.app)
    if str(row.status) == "awaiting_approval":
        waiting = await ApprovalService(runtime).waiting_request(row)
        if waiting:
            view.approval = ApprovalRequestView.model_validate(waiting)
    view.reply = await stored_reply(runtime, row)
    return view


@router.get("/metrics", response_model=MetricsResponse, summary="Counts and timings")
async def metrics(request: Request, principal: CurrentUser) -> MetricsResponse:
    return await records_service.metrics_view(session_factory_for(request), principal)
