"""Approvals: a person's decision on a paused workflow, recorded and then resumed.

``ApprovalService.decide`` (``POST /api/approval/{workflow_id}``) does, in order:

1. checks the workflow exists, is waiting for approval, and that the person deciding
   works at that shop. Only the owner may approve an action that needs the owner;
   anyone at the shop may reject;
2. checks each decision: every waiting action gets one (or one decision covers all),
   and changed details ("modify") must pass the tool's own input validation;
3. checks that the paused run can still be resumed (its checkpoint is in the
   database; with CHECKPOINTER=memory a restart loses it);
4. writes the decision **before** resuming: each ``approvals`` row gets its status,
   who decided, when and the final action, an ``audit_logs`` row is added per action,
   and the workflow is marked running again, all in one transaction. A crash after
   this point loses nothing: the decision is on record;
5. resumes the graph with ``Command(resume=...)``. The human-review node applies the
   decision, the action agent runs what was approved, and the reply is written and
   validated as usual. The reply is returned in the same shape as ``POST /api/chat``.

``ApprovalService.status`` (``GET /api/approval/{workflow_id}``) shows what is waiting,
for example after a restart or on a second device.
"""

from typing import Any

from langgraph.types import Command
from pydantic import ValidationError
from sqlalchemy import select

from app.core.exceptions import ConflictError, ForbiddenError, InvalidRequestError, NotFoundError
from app.core.logging import get_logger
from app.models import Approval, AuditLog, User, Workflow
from app.models.schemas import (
    ApprovalDecisionRequest,
    ApprovalRecordView,
    ApprovalRequestView,
    ApprovalStatusResponse,
    ChatResponse,
)
from app.models.types import utcnow
from app.services.chat_service import (
    AgentRuntime,
    finish,
    graph_config,
    make_deps,
    paused_request,
    run_graph,
)

logger = get_logger(__name__)

STATUS_FOR = {"approve": "approved", "reject": "rejected", "modify": "modified"}


class ApprovalService:
    def __init__(self, runtime: AgentRuntime) -> None:
        self.rt = runtime

    async def _paused_request(self, workflow: Workflow) -> dict[str, Any] | None:
        config = graph_config(workflow.id, workflow.session_id or "", workflow.shop_id)
        snapshot = await self.rt.graph.aget_state(config)
        if "human_review" not in (snapshot.next or ()):
            return None
        for interrupt in snapshot.interrupts or ():
            value = getattr(interrupt, "value", None)
            if isinstance(value, dict) and value.get("type") == "approval_request":
                return value
        return None

    async def status(self, workflow_id: str) -> ApprovalStatusResponse:
        async with self.rt.session_factory() as db:
            workflow = await db.get(Workflow, workflow_id)
            if workflow is None:
                raise NotFoundError(f"No workflow {workflow_id}.")
            rows = (
                await db.scalars(
                    select(Approval)
                    .where(Approval.workflow_id == workflow_id)
                    .order_by(Approval.created_at)
                )
            ).all()
        request = (
            await self._paused_request(workflow)
            if str(workflow.status) == "awaiting_approval"
            else None
        )
        return ApprovalStatusResponse(
            workflow_id=workflow_id,
            workflow_status=str(workflow.status),
            approval=ApprovalRequestView.model_validate(request) if request else None,
            approvals=[
                ApprovalRecordView(
                    approval_id=row.id,
                    tool=row.action_type,
                    status=str(row.status),
                    required_role=(row.proposed_action or {}).get("required_role"),
                    decided_by=row.decided_by,
                    decided_at=row.decided_at.isoformat() if row.decided_at else None,
                    decision_note=row.decision_note,
                )
                for row in rows
            ],
        )

    def _choices(
        self, rows: list[Approval], body: ApprovalDecisionRequest
    ) -> dict[str, tuple[str, dict[str, Any] | None]]:
        """approval_id -> (decision, changed arguments), for every waiting action."""
        by_id = {d.approval_id: d for d in body.decisions}
        unknown = sorted(set(by_id) - {row.id for row in rows})
        if unknown:
            raise InvalidRequestError(
                f"These approvals are not waiting in this workflow: {', '.join(unknown)}."
            )
        choices: dict[str, tuple[str, dict[str, Any] | None]] = {}
        for row in rows:
            if row.id in by_id:
                choices[row.id] = (by_id[row.id].decision, by_id[row.id].arguments)
            elif body.decision is not None and body.decision != "modify":
                choices[row.id] = (body.decision, None)
            else:
                raise InvalidRequestError(
                    "Give a decision for every waiting action (decisions), or one decision "
                    "for all of them (decision: approve or reject). Changing details "
                    "(modify) needs the new arguments for that action."
                )
        return choices

    def _check_arguments(self, row: Approval, arguments: dict[str, Any] | None) -> None:
        if not arguments:
            raise InvalidRequestError(f"modify needs the new arguments for {row.action_type}.")
        spec = self.rt.registry.spec(row.action_type)
        if spec is None:
            raise InvalidRequestError(f"Unknown action {row.action_type}.")
        trial = {**arguments}
        if "idempotency_key" in spec.input_model.model_fields:
            trial["idempotency_key"] = "check-only-key"
        try:
            spec.input_model.model_validate(trial)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                for e in exc.errors(include_url=False)[:4]
            )
            raise InvalidRequestError(
                f"The changed details for {row.action_type} are not valid: {problems}."
            ) from exc

    async def decide(self, workflow_id: str, body: ApprovalDecisionRequest) -> ChatResponse:
        async with self.rt.session_factory() as db:
            workflow = await db.get(Workflow, workflow_id, with_for_update=True)
            if workflow is None:
                raise NotFoundError(f"No workflow {workflow_id}.")
            user = await db.get(User, body.user_id)
            if user is None or user.shop_id != workflow.shop_id or not user.is_active:
                raise ForbiddenError(f"User {body.user_id} does not work at this shop.")
            if str(workflow.status) != "awaiting_approval":
                raise ConflictError(
                    f"Workflow {workflow_id} is not waiting for approval "
                    f"(it is {workflow.status}).",
                    details={"workflow_status": str(workflow.status)},
                )
            rows = list(
                (
                    await db.scalars(
                        select(Approval)
                        .where(Approval.workflow_id == workflow_id, Approval.status == "pending")
                        .order_by(Approval.created_at)
                    )
                ).all()
            )
            if not rows:
                raise ConflictError(f"Nothing in workflow {workflow_id} is waiting for approval.")
            choices = self._choices(rows, body)
            role = str(user.role)
            for row in rows:
                decision, arguments = choices[row.id]
                needed = (row.proposed_action or {}).get("required_role")
                if decision != "reject" and needed == "owner" and role != "owner":
                    raise ForbiddenError(
                        f"Only the shop owner can approve {row.action_type}: "
                        f"{row.reason} {user.name} can reject it, or ask the owner.",
                        details={"approval_id": row.id, "required_role": "owner"},
                    )
                if decision == "modify":
                    self._check_arguments(row, arguments)
            if await self._paused_request(workflow) is None:
                raise ConflictError(
                    f"Workflow {workflow_id} can no longer be resumed: its saved state is "
                    "gone (CHECKPOINTER=memory and a restart?). Ask the question again.",
                )

            decided_at = utcnow()
            resume: dict[str, Any] = {
                "decided_by": user.id,
                "role": role,
                "note": body.note,
                "decided_at": decided_at.isoformat(),
                "decisions": {},
            }
            for row in rows:
                decision, arguments = choices[row.id]
                proposed = row.proposed_action or {}
                final = {
                    "tool": row.action_type,
                    "arguments": arguments if decision == "modify" else proposed.get("arguments"),
                }
                row.status = STATUS_FOR[decision]
                row.decided_by = user.id
                row.decision_note = body.note
                row.decided_at = decided_at
                row.final_action = None if decision == "reject" else final
                db.add(
                    AuditLog(
                        actor_type="user",
                        actor_id=user.id,
                        action=f"approval_{STATUS_FOR[decision]}",
                        entity_type="approval",
                        entity_id=row.id[:40],
                        shop_id=workflow.shop_id,
                        workflow_id=workflow_id,
                        details={
                            "tool": row.action_type,
                            "role": role,
                            "required_role": proposed.get("required_role"),
                            "note": body.note,
                            **({"arguments": arguments} if decision == "modify" else {}),
                        },
                    )
                )
                resume["decisions"][proposed.get("action_id")] = {
                    "decision": decision,
                    "arguments": arguments,
                    "approval_id": row.id,
                }
            workflow.status = "running"
            await db.commit()
            session_id, shop_id = workflow.session_id or "", workflow.shop_id

        logger.info(
            "approval_decided",
            workflow_id=workflow_id,
            decided_by=user.id,
            role=role,
            decisions={k: v["decision"] for k, v in resume["decisions"].items()},
        )
        summary = ", ".join(
            f"{v['decision']} {k.split(':')[0]}" for k, v in resume["decisions"].items()
        )
        if session_id:
            await self.rt.memory.append(
                session_id, "user", f"[decision by {user.id}] {summary}", workflow_id=workflow_id
            )
        config = graph_config(workflow_id, session_id, shop_id)
        base = {"workflow_id": workflow_id, "session_id": session_id, "shop_id": shop_id}
        final = await run_graph(self.rt, Command(resume=resume), config, make_deps(self.rt), base)
        if paused_request(final) is None and not final.get("final_response"):
            final.setdefault("outcome", "failed")
        return await finish(self.rt, final, workflow_id, session_id)
