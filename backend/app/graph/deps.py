"""What the agents need to do their work, handed to the graph for one run.

LangGraph passes this object to every node as its *runtime context*
(``Runtime[AgentDeps]``). Unlike the state, it is never saved in a checkpoint, so it
can hold live objects: the model client, the tool registry, database sessions and the
knowledge retriever. Tests build one with a fake model and a SQLite database.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.core.logging import get_logger
from app.models import AgentRun
from app.models.types import utcnow
from app.rag.retriever import KnowledgeRetriever
from app.services.chat_model import ChatModel
from app.tools.base import ApprovalGrant, ToolContext, ToolResult
from app.tools.registry import ToolRegistry

logger = get_logger(__name__)

EventSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class AgentDeps:
    settings: Settings
    llm: ChatModel
    registry: ToolRegistry
    session_factory: async_sessionmaker[AsyncSession]
    now: datetime  # the business clock (BUSINESS_DATE in development)
    knowledge: KnowledgeRetriever | None = None
    clients: dict[str, Any] = field(default_factory=dict)  # mock supplier/messaging APIs
    # Write agent_runs and tool_calls rows. Needs the workflows row to exist (the chat
    # service creates it first); tests that run the graph alone switch it off.
    record_to_db: bool = True
    on_event: EventSink | None = None  # progress events (the SSE stream in Phase 6)

    def tool_context(
        self,
        session: AsyncSession,
        state: dict[str, Any],
        agent: str,
        approval: ApprovalGrant | None = None,
    ) -> ToolContext:
        clients = dict(self.clients)
        if self.knowledge is not None:
            clients["knowledge"] = self.knowledge
        return ToolContext(
            session=session,
            shop_id=state["shop_id"],
            actor=agent,
            now=self.now,
            workflow_id=state.get("workflow_id"),
            approval=approval,  # only the action agent passes one, from a recorded decision
            log_session_factory=self.session_factory if self.record_to_db else None,
            clients=clients,
        )

    async def call_tool(
        self,
        session: AsyncSession,
        state: dict[str, Any],
        agent: str,
        name: str,
        arguments: dict[str, Any],
        *,
        approval: ApprovalGrant | None = None,
    ) -> ToolResult:
        ctx = self.tool_context(session, state, agent, approval)
        return await self.registry.call(name, arguments, ctx, agent=agent)

    async def emit(self, event: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            await self.on_event(event)
        except Exception as exc:  # a broken listener must not break the workflow
            logger.warning("event_sink_failed", error=repr(exc))

    async def record_run(
        self, workflow_id: str | None, run: dict[str, Any], started_at: datetime
    ) -> None:
        """Save one agent run to agent_runs. Never raises: logging must not break a run."""
        if not self.record_to_db or not workflow_id:
            return
        try:
            async with self.session_factory() as session:
                session.add(
                    AgentRun(
                        workflow_id=workflow_id,
                        agent=run["agent"],
                        model=run.get("model") or None,
                        status="success" if run["status"] == "success" else "error",
                        latency_ms=run.get("latency_ms"),
                        input_tokens=run.get("input_tokens"),
                        output_tokens=run.get("output_tokens"),
                        summary=(run.get("summary") or "")[:2000] or None,
                        error=(run.get("error") or "")[:2000] or None,
                        started_at=started_at,
                        finished_at=utcnow(),
                    )
                )
                await session.commit()
        except Exception as exc:
            logger.warning("agent_run_log_failed", agent=run.get("agent"), error=repr(exc))
