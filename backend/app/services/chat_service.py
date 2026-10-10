"""One chat turn, from request to reply: the glue around the agent graph.

``ChatService.handle`` does, in order:

1. checks that the shop exists, that the user (if given) works there, and that the
   conversation (if continued) belongs to the same shop;
2. creates the conversation (``sessions``) on first use and a ``workflows`` row for
   this request;
3. loads the conversation's last messages (short-term memory) and saves the new one;
4. runs the agent graph with the workflow ID as its thread, so every step is
   checkpointed under that ID;
5. saves the reply and the workflow's outcome, and returns everything the frontend
   shows: the answer, its sources, the proposed actions, every tool call and every
   agent step with its model, time and tokens.

When an action needs a person, the graph pauses (Phase 5). The reply then has status
``awaiting_approval`` and an ``approval`` block saying what to decide; the workflow row
stays ``awaiting_approval`` until ``POST /api/approval/{workflow_id}`` resumes it
(app/services/approval_service.py, which reuses the helpers below).

The pieces that are expensive to build (the model client, the compiled graph, the
knowledge retriever) are built once, on the first chat request, by ``get_agent_runtime``.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.core.exceptions import DependencyUnavailableError, ForbiddenError, NotFoundError
from app.core.logging import get_logger
from app.graph.deps import AgentDeps
from app.graph.workflow import RECURSION_LIMIT, compile_graph, make_checkpointer
from app.models import ChatSession, Shop, User, Workflow
from app.models.schemas import (
    AgentStep,
    ApprovalRequestView,
    ChatRequest,
    ChatResponse,
    ProposedActionView,
    ReplyDetails,
    SourceView,
    ToolActivity,
    UsageTotals,
)
from app.models.types import utcnow
from app.rag.retriever import KnowledgeRetriever
from app.services.chat_model import ChatModel, OpenAIChatModel, chat_model_problem
from app.services.knowledge_service import shared_retriever
from app.services.memory_service import ConversationMemory
from app.tools.api_tools import default_clients
from app.tools.base import business_now
from app.tools.registry import ToolRegistry

logger = get_logger(__name__)

FAILED_REPLY = (
    "Sorry, something went wrong while working on this request, and nothing was changed. "
    "Please try again in a minute."
)


@dataclass
class AgentRuntime:
    settings: Settings
    llm: ChatModel
    registry: ToolRegistry
    graph: CompiledStateGraph
    memory: ConversationMemory
    session_factory: async_sessionmaker[AsyncSession]
    clients: dict[str, Any]
    knowledge: KnowledgeRetriever | None


async def get_agent_runtime(app: FastAPI) -> AgentRuntime:
    """The app's AgentRuntime, built on the first chat request."""
    state = app.state
    if getattr(state, "agent_runtime", None) is not None:
        return state.agent_runtime
    async with state.agent_lock:
        if getattr(state, "agent_runtime", None) is None:
            state.agent_runtime = await _build_runtime(app)
    return state.agent_runtime


async def _build_runtime(app: FastAPI) -> AgentRuntime:
    from app.models.database import get_session_factory
    from app.services.redis_client import get_redis

    settings: Settings = app.state.settings
    problem = chat_model_problem(settings)
    if problem:
        raise DependencyUnavailableError(problem)
    session_factory = get_session_factory()
    try:
        knowledge: KnowledgeRetriever | None = await shared_retriever(app)
    except DependencyUnavailableError as exc:
        # The agents still answer from the shop's records; the knowledge agent's tool
        # reports that the documents are unavailable, and the reply says so.
        logger.warning("knowledge_unavailable_for_agents", error=exc.message)
        knowledge = None
    try:
        redis = get_redis()
    except RuntimeError:
        redis = None
    return AgentRuntime(
        settings=settings,
        llm=OpenAIChatModel(settings),
        registry=ToolRegistry(retry_backoff_seconds=settings.tool_retry_backoff_seconds),
        graph=compile_graph(make_checkpointer(settings, session_factory)),
        memory=ConversationMemory(
            session_factory,
            redis,
            turns=settings.session_memory_turns,
            ttl_seconds=settings.session_ttl_hours * 3600,
        ),
        session_factory=session_factory,
        clients=default_clients(settings),
        knowledge=knowledge,
    )


def make_deps(rt: AgentRuntime) -> AgentDeps:
    return AgentDeps(
        settings=rt.settings,
        llm=rt.llm,
        registry=rt.registry,
        session_factory=rt.session_factory,
        now=business_now(rt.settings.business_date),
        knowledge=rt.knowledge,
        clients=rt.clients,
    )


def graph_config(workflow_id: str, session_id: str, shop_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": workflow_id},
        "recursion_limit": RECURSION_LIMIT,
        "run_name": "orm_ai_chat",
        "tags": [shop_id],
        "metadata": {"workflow_id": workflow_id, "session_id": session_id},
    }


async def run_graph(
    rt: AgentRuntime, payload: Any, config: dict[str, Any], deps: AgentDeps, base: dict[str, Any]
) -> dict[str, Any]:
    """Run (or resume) the graph. Never raises: a crash becomes a failed outcome."""
    try:
        return await rt.graph.ainvoke(payload, config=config, context=deps)
    except Exception as exc:
        # Agent failures are handled inside the graph; reaching here means the graph
        # itself could not run (for example the database went away mid-run).
        logger.exception("workflow_crashed", workflow_id=config["configurable"]["thread_id"])
        return {
            **base,
            "final_response": FAILED_REPLY,
            "outcome": "failed",
            "errors": [f"The workflow stopped ({type(exc).__name__})."],
        }


def paused_request(final: dict[str, Any]) -> dict[str, Any] | None:
    """The approval request when the run paused for a person, else None."""
    for item in final.get("__interrupt__") or []:
        value = getattr(item, "value", item)
        if isinstance(value, dict) and value.get("type") == "approval_request":
            return value
    return None


async def close_workflow(rt: AgentRuntime, workflow_id: str, response: ChatResponse) -> None:
    async with rt.session_factory() as db:
        workflow = await db.get(Workflow, workflow_id)
        if workflow is None:
            return
        status = {
            "failed": "failed",
            "awaiting_approval": "awaiting_approval",
            "blocked": "blocked",
        }.get(response.status, "completed")
        workflow.status = status
        workflow.intent = response.intent
        workflow.final_response = response.answer
        workflow.error = response.errors[0][:2000] if response.errors else None
        workflow.completed_at = None if status == "awaiting_approval" else utcnow()
        await db.commit()


async def finish(
    rt: AgentRuntime, final: dict[str, Any], workflow_id: str, session_id: str
) -> ChatResponse:
    response = build_response(final, workflow_id, session_id)
    await close_workflow(rt, workflow_id, response)
    await rt.memory.append(session_id, "assistant", response.answer, workflow_id=workflow_id)
    return response


class ChatService:
    def __init__(self, runtime: AgentRuntime) -> None:
        self.rt = runtime

    async def _open_workflow(self, request: ChatRequest) -> tuple[str, str]:
        """(session_id, workflow_id), after checking who may use which records."""
        async with self.rt.session_factory() as db:
            if await db.get(Shop, request.shop_id) is None:
                raise NotFoundError(f"No shop with ID {request.shop_id}.")
            if request.user_id:
                user = await db.get(User, request.user_id)
                if user is None or user.shop_id != request.shop_id:
                    raise ForbiddenError(f"User {request.user_id} does not work at this shop.")
            session_id = request.session_id or str(uuid.uuid4())
            conversation = await db.get(ChatSession, session_id)
            if conversation is None:
                db.add(
                    ChatSession(
                        id=session_id,
                        shop_id=request.shop_id,
                        user_id=request.user_id,
                        title=request.message[:200],
                    )
                )
            elif conversation.shop_id != request.shop_id:
                # Same answer as "does not exist", so conversation IDs reveal nothing.
                raise NotFoundError(f"No conversation {session_id} for this shop.")
            else:
                conversation.last_active_at = utcnow()
            workflow_id = str(uuid.uuid4())
            db.add(
                Workflow(
                    id=workflow_id,
                    session_id=session_id,
                    shop_id=request.shop_id,
                    user_query=request.message,
                    status="running",
                )
            )
            await db.commit()
        return session_id, workflow_id

    async def handle(self, request: ChatRequest) -> ChatResponse:
        session_id, workflow_id = await self._open_workflow(request)
        history = await self.rt.memory.recent(session_id)
        await self.rt.memory.append(session_id, "user", request.message, workflow_id=workflow_id)

        deps = make_deps(self.rt)
        initial = {
            "workflow_id": workflow_id,
            "session_id": session_id,
            "user_id": request.user_id,
            "shop_id": request.shop_id,
            "user_query": request.message,
            "conversation_history": [{"role": m["role"], "content": m["content"]} for m in history],
        }
        config = graph_config(workflow_id, session_id, request.shop_id)
        final = await run_graph(self.rt, initial, config, deps, initial)
        return await finish(self.rt, final, workflow_id, session_id)


def _action_views(
    final: dict[str, Any], request: dict[str, Any] | None
) -> list[ProposedActionView]:
    if request is not None:  # paused: the node's update is not in the state yet
        return [
            ProposedActionView(
                action_id=a["action_id"],
                tool=a["tool"],
                arguments=a["arguments"],
                reason=a["reason"],
                status="awaiting_approval",
                required_role=a["required_role"],
                approval_id=a["approval_id"],
            )
            for a in request["actions"]
        ]
    return [
        ProposedActionView(
            action_id=a.get("action_id"),
            tool=a["tool"],
            arguments={k: v for k, v in a["arguments"].items() if k != "idempotency_key"},
            reason=a["reason"],
            status=a["status"],
            required_role=a.get("required_role"),
            approval_id=a.get("approval_id"),
            result=a.get("result"),
        )
        for a in final.get("proposed_actions") or []
    ]


def _paused_sources(final: dict[str, Any], request: dict[str, Any]) -> list[SourceView]:
    passages = {p["citation"]: p for p in final.get("retrieved_documents") or []}
    return [
        SourceView(
            citation=c,
            title=passages[c]["title"],
            section=passages[c]["section"],
            excerpt=passages[c]["text"][:300],
            trust=passages[c]["trust"],
        )
        for c in request.get("policy_references") or []
        if c in passages
    ]


def build_response(final: dict[str, Any], workflow_id: str, session_id: str) -> ChatResponse:
    draft = final.get("draft_response") or {}
    trace = final.get("agent_trace") or []
    request = paused_request(final)
    if request is not None:
        status, answer = "awaiting_approval", request["message"]
        sources = _paused_sources(final, request)
        approval: ApprovalRequestView | None = ApprovalRequestView.model_validate(request)
    else:
        status = final.get("outcome") or "failed"
        answer = final.get("final_response") or FAILED_REPLY
        sources = [SourceView(**source) for source in final.get("sources") or []]
        approval = None
    return ChatResponse(
        workflow_id=workflow_id,
        session_id=session_id,
        status=status,
        intent=final.get("intent"),
        answer=answer,
        details=ReplyDetails(
            facts=draft.get("facts", []),
            evidence=draft.get("evidence", []),
            next_steps=draft.get("next_steps", []),
            pending_approval=draft.get("pending_approval", []),
            completed_actions=draft.get("completed_actions", []),
            follow_up_question=draft.get("follow_up_question"),
        ),
        sources=sources,
        proposed_actions=_action_views(final, request),
        tool_calls=[
            ToolActivity(
                agent=c["agent"],
                tool=c["tool"],
                status=c["status"],
                error_code=c.get("error_code"),
                latency_ms=c.get("latency_ms"),
            )
            for c in final.get("tool_results") or []
        ],
        agents=[
            AgentStep(
                agent=t["agent"],
                status=t["status"],
                model=t.get("model"),
                latency_ms=t["latency_ms"],
                input_tokens=t.get("input_tokens") or 0,
                output_tokens=t.get("output_tokens") or 0,
                summary=t["summary"],
                error=t.get("error"),
            )
            for t in trace
        ],
        usage=UsageTotals(
            input_tokens=sum(t.get("input_tokens") or 0 for t in trace),
            output_tokens=sum(t.get("output_tokens") or 0 for t in trace),
            llm_calls=sum(t.get("llm_calls") or 0 for t in trace),
            latency_ms=round(sum(t["latency_ms"] for t in trace), 1),
        ),
        validation=final.get("validation_result"),
        warnings=list(dict.fromkeys(final.get("warnings") or [])),
        errors=list(dict.fromkeys(final.get("errors") or [])),
        approval=approval,
    )
