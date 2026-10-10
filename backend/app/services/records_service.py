"""Read-only views of what the agents did: conversations, workflows and metrics.

These power ``GET /api/sessions/{id}``, ``GET /api/workflows/{id}`` and
``GET /api/metrics`` (Phase 6), and the lists the frontend needs (Phase 7):
``GET /api/sessions`` (a user's recent conversations), ``GET /api/workflows`` (recent
requests), ``GET /api/approvals`` (the approvals inbox) and ``GET /api/evaluations``
(evaluation runs). They only read. Every view is limited to one shop when the caller
is logged in as a shop user; admins (and local development without a token) see
everything, or one shop when they ask for it.
"""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.human_review import describe
from app.core.auth import Principal, check_shop
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models import (
    AgentRun,
    Approval,
    ChatSession,
    Document,
    DocumentChunk,
    Evaluation,
    Message,
    Shop,
    ToolCall,
    Workflow,
)
from app.models.schemas import (
    AgentMetrics,
    AgentRunView,
    ApprovalListItem,
    ApprovalListResponse,
    ApprovalRecordView,
    EvaluationCaseView,
    EvaluationRunView,
    EvaluationsResponse,
    MessageView,
    MetricsResponse,
    SessionListItem,
    SessionListResponse,
    SessionResponse,
    ShopListResponse,
    ShopView,
    ToolCallView,
    ToolMetrics,
    WorkflowListItem,
    WorkflowListResponse,
    WorkflowResponse,
    WorkflowSummary,
)
from app.models.types import utcnow


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _summary(workflow: Workflow) -> WorkflowSummary:
    return WorkflowSummary(
        workflow_id=workflow.id,
        status=str(workflow.status),
        intent=workflow.intent,
        user_query=workflow.user_query,
        created_at=_iso(workflow.created_at) or "",
        completed_at=_iso(workflow.completed_at),
    )


def approval_views(rows: list[Approval]) -> list[ApprovalRecordView]:
    return [
        ApprovalRecordView(
            approval_id=row.id,
            tool=row.action_type,
            status=str(row.status),
            required_role=(row.proposed_action or {}).get("required_role"),
            decided_by=row.decided_by,
            decided_at=_iso(row.decided_at),
            decision_note=row.decision_note,
        )
        for row in rows
    ]


async def session_view(
    factory: async_sessionmaker[AsyncSession], session_id: str, principal: Principal | None
) -> SessionResponse:
    async with factory() as db:
        conversation = await db.get(ChatSession, session_id)
        if conversation is None:
            raise NotFoundError(f"No conversation {session_id}.")
        check_shop(principal, conversation.shop_id)
        messages = (
            await db.scalars(
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(Message.created_at, Message.id)
            )
        ).all()
        workflows = (
            await db.scalars(
                select(Workflow)
                .where(Workflow.session_id == session_id)
                .order_by(Workflow.created_at)
            )
        ).all()
    return SessionResponse(
        session_id=conversation.id,
        shop_id=conversation.shop_id,
        user_id=conversation.user_id,
        title=conversation.title,
        created_at=_iso(conversation.created_at) or "",
        last_active_at=_iso(conversation.last_active_at) or "",
        messages=[
            MessageView(
                role=str(m.role),
                content=m.content,
                created_at=_iso(m.created_at) or "",
                workflow_id=m.workflow_id,
            )
            for m in messages
        ],
        workflows=[_summary(w) for w in workflows],
    )


async def workflow_view(
    factory: async_sessionmaker[AsyncSession], workflow_id: str, principal: Principal | None
) -> tuple[WorkflowResponse, Workflow]:
    async with factory() as db:
        workflow = await db.get(Workflow, workflow_id)
        if workflow is None:
            raise NotFoundError(f"No workflow {workflow_id}.")
        check_shop(principal, workflow.shop_id)
        runs = (
            await db.scalars(
                select(AgentRun)
                .where(AgentRun.workflow_id == workflow_id)
                .order_by(AgentRun.started_at, AgentRun.id)
            )
        ).all()
        calls = (
            await db.scalars(
                select(ToolCall)
                .where(ToolCall.workflow_id == workflow_id)
                .order_by(ToolCall.created_at, ToolCall.id)
            )
        ).all()
        approvals = (
            await db.scalars(
                select(Approval)
                .where(Approval.workflow_id == workflow_id)
                .order_by(Approval.created_at)
            )
        ).all()
    response = WorkflowResponse(
        workflow_id=workflow.id,
        session_id=workflow.session_id,
        shop_id=workflow.shop_id,
        status=str(workflow.status),
        intent=workflow.intent,
        user_query=workflow.user_query,
        final_response=workflow.final_response,
        error=workflow.error,
        created_at=_iso(workflow.created_at) or "",
        updated_at=_iso(workflow.updated_at),
        completed_at=_iso(workflow.completed_at),
        agents=[
            AgentRunView(
                agent=r.agent,
                status=str(r.status),
                model=r.model,
                latency_ms=r.latency_ms,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                summary=r.summary,
                error=r.error,
                started_at=_iso(r.started_at) or "",
            )
            for r in runs
        ],
        tool_calls=[
            ToolCallView(
                agent=c.agent,
                tool=c.tool_name,
                status=str(c.status),
                error_code=c.error_code,
                latency_ms=c.latency_ms,
                arguments={k: v for k, v in (c.arguments or {}).items() if k != "idempotency_key"},
                created_at=_iso(c.created_at) or "",
            )
            for c in calls
        ],
        approvals=approval_views(list(approvals)),
    )
    return response, workflow


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))], 1)


async def metrics_view(
    factory: async_sessionmaker[AsyncSession], principal: Principal | None
) -> MetricsResponse:
    shop = principal.shop_id if principal and principal.role != "admin" else None
    async with factory() as db:
        workflows = select(Workflow.id, Workflow.status, Workflow.created_at)
        if shop:
            workflows = workflows.where(Workflow.shop_id == shop)
        rows = (await db.execute(workflows)).all()
        by_status = Counter(str(r.status) for r in rows)
        since = utcnow() - timedelta(hours=24)

        def recent(value: datetime | None) -> bool:
            if value is None:
                return False
            if value.tzinfo is None:
                value = value.replace(tzinfo=since.tzinfo)
            return value >= since

        run_query = select(
            AgentRun.agent,
            AgentRun.status,
            AgentRun.latency_ms,
            AgentRun.input_tokens,
            AgentRun.output_tokens,
        )
        call_query = select(
            ToolCall.tool_name, ToolCall.status, ToolCall.error_code, ToolCall.latency_ms
        )
        approval_query = select(Approval.status, func.count()).group_by(Approval.status)
        if shop:
            shop_workflows = select(Workflow.id).where(Workflow.shop_id == shop)
            run_query = run_query.where(AgentRun.workflow_id.in_(shop_workflows))
            call_query = call_query.where(ToolCall.workflow_id.in_(shop_workflows))
            approval_query = approval_query.where(Approval.shop_id == shop)
        runs = (await db.execute(run_query)).all()
        calls = (await db.execute(call_query)).all()
        approvals = {str(status): count for status, count in (await db.execute(approval_query))}
        documents = await db.scalar(select(func.count()).select_from(Document)) or 0
        chunks = await db.scalar(select(func.count()).select_from(DocumentChunk)) or 0

    agents: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"runs": 0, "errors": 0, "latencies": [], "in": 0, "out": 0}
    )
    for r in runs:
        entry = agents[r.agent]
        entry["runs"] += 1
        entry["errors"] += str(r.status) != "success"
        if r.latency_ms is not None:
            entry["latencies"].append(r.latency_ms)
        entry["in"] += r.input_tokens or 0
        entry["out"] += r.output_tokens or 0
    tools: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "codes": Counter(), "latencies": []}
    )
    for c in calls:
        entry = tools[c.tool_name]
        entry["calls"] += 1
        entry["latencies"].append(c.latency_ms or 0.0)
        if str(c.status) != "success":
            entry["errors"] += 1
            entry["codes"][c.error_code or "unknown"] += 1
    return MetricsResponse(
        generated_at=utcnow().isoformat(),
        scope=shop or "all shops",
        workflows={"total": len(rows), **dict(by_status)},
        workflows_last_24h=sum(recent(r.created_at) for r in rows),
        agents=[
            AgentMetrics(
                agent=name,
                runs=e["runs"],
                errors=e["errors"],
                avg_latency_ms=round(sum(e["latencies"]) / len(e["latencies"]), 1)
                if e["latencies"]
                else 0.0,
                p95_latency_ms=_p95(e["latencies"]),
                input_tokens=e["in"],
                output_tokens=e["out"],
            )
            for name, e in sorted(agents.items())
        ],
        tools=[
            ToolMetrics(
                tool=name,
                calls=e["calls"],
                errors=e["errors"],
                error_codes=dict(e["codes"]),
                avg_latency_ms=round(sum(e["latencies"]) / len(e["latencies"]), 1)
                if e["latencies"]
                else 0.0,
            )
            for name, e in sorted(tools.items())
        ],
        approvals={"pending": 0, "approved": 0, "rejected": 0, "modified": 0, **approvals},
        documents={"documents": int(documents), "chunks": int(chunks)},
    )


# ---------------------------------------------------------------- Phase 7: lists


def scope_shop(principal: Principal | None, shop_id: str | None) -> str | None:
    """The one shop a list covers, or None for every shop.

    A shop user always gets their own shop (asking for another is refused); an admin,
    or local development without a token, gets the shop asked for, or every shop.
    """
    if principal is not None and principal.role != "admin":
        if shop_id is not None:
            check_shop(principal, shop_id)
        return principal.shop_id
    return shop_id


async def list_shops(
    factory: async_sessionmaker[AsyncSession], principal: Principal | None
) -> ShopListResponse:
    """The shops the caller can ask about: their own, or every shop for an admin."""
    shop = scope_shop(principal, None)
    async with factory() as db:
        query = select(Shop).order_by(Shop.id)
        if shop:
            query = query.where(Shop.id == shop)
        rows = (await db.scalars(query)).all()
    return ShopListResponse(
        shops=[
            ShopView(
                shop_id=s.id,
                name=s.name,
                shop_type=str(s.shop_type),
                locality=s.locality,
                city=s.city,
            )
            for s in rows
        ]
    )


async def list_sessions(
    factory: async_sessionmaker[AsyncSession],
    principal: Principal | None,
    *,
    shop_id: str | None = None,
    limit: int = 20,
) -> SessionListResponse:
    """The caller's recent conversations, newest first.

    A shop user sees only the conversations they started; an admin sees everyone's
    (in one shop when ``shop_id`` is given).
    """
    shop = scope_shop(principal, shop_id)
    async with factory() as db:
        query = select(ChatSession).order_by(ChatSession.last_active_at.desc()).limit(limit)
        if shop:
            query = query.where(ChatSession.shop_id == shop)
        if principal is not None and principal.role != "admin":
            query = query.where(ChatSession.user_id == principal.user_id)
        sessions = (await db.scalars(query)).all()
        ids = [s.id for s in sessions]
        counts: dict[str, int] = {}
        latest: dict[str, str] = {}
        if ids:
            rows = (
                await db.execute(
                    select(Workflow.session_id, Workflow.status, Workflow.created_at)
                    .where(Workflow.session_id.in_(ids))
                    .order_by(Workflow.created_at)
                )
            ).all()
            for row in rows:
                counts[row.session_id] = counts.get(row.session_id, 0) + 1
                latest[row.session_id] = str(row.status)
    return SessionListResponse(
        sessions=[
            SessionListItem(
                session_id=s.id,
                shop_id=s.shop_id,
                user_id=s.user_id,
                title=s.title,
                created_at=_iso(s.created_at) or "",
                last_active_at=_iso(s.last_active_at) or "",
                workflows=counts.get(s.id, 0),
                last_status=latest.get(s.id),
            )
            for s in sessions
        ]
    )


async def list_workflows(
    factory: async_sessionmaker[AsyncSession],
    principal: Principal | None,
    *,
    shop_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> WorkflowListResponse:
    """Recent requests, newest first (the admin page's list)."""
    shop = scope_shop(principal, shop_id)
    async with factory() as db:
        query = select(Workflow).order_by(Workflow.created_at.desc(), Workflow.id).limit(limit)
        if shop:
            query = query.where(Workflow.shop_id == shop)
        if status:
            query = query.where(Workflow.status == status)
        rows = (await db.scalars(query)).all()
    return WorkflowListResponse(
        workflows=[
            WorkflowListItem(**_summary(w).model_dump(), shop_id=w.shop_id, session_id=w.session_id)
            for w in rows
        ]
    )


async def list_approvals(
    factory: async_sessionmaker[AsyncSession],
    principal: Principal | None,
    *,
    shop_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> ApprovalListResponse:
    """The approvals inbox: what waits for a person (or was decided), newest first."""
    shop = scope_shop(principal, shop_id)
    async with factory() as db:
        query = (
            select(Approval, Workflow.user_query, Workflow.status)
            .join(Workflow, Workflow.id == Approval.workflow_id, isouter=True)
            .order_by(Approval.created_at.desc(), Approval.id)
            .limit(limit)
        )
        count_query = select(Approval.status, func.count()).group_by(Approval.status)
        if shop:
            query = query.where(Approval.shop_id == shop)
            count_query = count_query.where(Approval.shop_id == shop)
        if status:
            query = query.where(Approval.status == status)
        rows = (await db.execute(query)).all()
        counts = {str(s): int(n) for s, n in await db.execute(count_query)}
    items = []
    for approval, user_query, workflow_status in rows:
        proposed = approval.proposed_action or {}
        arguments = {
            k: v for k, v in (proposed.get("arguments") or {}).items() if k != "idempotency_key"
        }
        items.append(
            ApprovalListItem(
                approval_id=approval.id,
                workflow_id=approval.workflow_id,
                shop_id=approval.shop_id,
                tool=approval.action_type,
                description=describe({"tool": approval.action_type, "arguments": arguments}),
                arguments=arguments,
                reason=approval.reason,
                required_role=proposed.get("required_role"),
                approval_reasons=list(proposed.get("approval_reasons") or []),
                estimate=proposed.get("estimate"),
                confidence=approval.confidence,
                policy_references=[str(p) for p in approval.policy_refs or []],
                status=str(approval.status),
                user_query=user_query,
                workflow_status=str(workflow_status) if workflow_status else None,
                created_at=_iso(approval.created_at) or "",
                decided_by=approval.decided_by,
                decided_at=_iso(approval.decided_at),
                decision_note=approval.decision_note,
            )
        )
    return ApprovalListResponse(
        approvals=items,
        counts={"pending": 0, "approved": 0, "modified": 0, "rejected": 0, **counts},
    )


async def list_evaluations(
    factory: async_sessionmaker[AsyncSession], principal: Principal | None, *, limit: int = 5
) -> EvaluationsResponse:
    """The latest evaluation runs (``python -m scripts.eval_agent`` saves them).

    Evaluation cases cover many shops, so only admins (or local development without a
    token) see them.
    """
    if principal is not None and principal.role != "admin":
        raise ForbiddenError(
            "Evaluation results cover every shop, so only an admin can see them "
            "(in development, log in as USR-101)."
        )
    async with factory() as db:
        latest = (
            select(Evaluation.run_id, func.max(Evaluation.created_at).label("at"))
            .group_by(Evaluation.run_id)
            .order_by(func.max(Evaluation.created_at).desc())
            .limit(limit)
        )
        runs = (await db.execute(latest)).all()
        run_ids = [r.run_id for r in runs]
        rows = (
            (
                await db.scalars(
                    select(Evaluation).where(Evaluation.run_id.in_(run_ids)).order_by(Evaluation.id)
                )
            ).all()
            if run_ids
            else []
        )
    by_run: dict[str, list[Evaluation]] = defaultdict(list)
    for row in rows:
        by_run[row.run_id].append(row)
    views = []
    for run in runs:
        cases = by_run[run.run_id]
        totals: dict[str, list[float]] = defaultdict(list)
        for case in cases:
            for name, value in (case.scores or {}).items():
                if isinstance(value, int | float) and not isinstance(value, bool):
                    totals[name].append(float(value))
                elif isinstance(value, bool):
                    totals[name].append(1.0 if value else 0.0)
        views.append(
            EvaluationRunView(
                run_id=run.run_id,
                created_at=_iso(run.at) or "",
                models=next(((c.details or {}).get("models") for c in cases if c.details), None),
                cases=len(cases),
                passed=sum(1 for c in cases if c.passed),
                scores={k: round(sum(v) / len(v), 3) for k, v in totals.items() if v},
                results=[
                    EvaluationCaseView(
                        case_id=c.case_id,
                        category=c.category,
                        passed=c.passed,
                        scores={
                            k: float(v)
                            for k, v in (c.scores or {}).items()
                            if isinstance(v, int | float)
                        },
                        latency_ms=c.latency_ms,
                        details=c.details or {},
                    )
                    for c in cases
                ],
            )
        )
    return EvaluationsResponse(runs=views)
