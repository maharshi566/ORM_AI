"""Builds the agent graph and compiles it with a checkpointer.

    START -> triage -> supervisor -+-> data_retrieval -> supervisor
                                   +-> knowledge      -> supervisor
                                   +-> investigation -+-> data_retrieval (need more data)
                                   |                  +-> human_review -> respond
                                   |                  +-> respond
                                   +-> respond -> validate -+-> respond (RETRY)
                                   |                        +-> finalize -> END
                                   +-> clarify -> finalize
                                   +-> finalize

The supervisor is the hub: every specialist reports back to it, and it decides who
runs next. ``docs/agent-graph.md`` shows the same graph as a Mermaid diagram, drawn
from this code by ``python -m scripts.draw_graph``.
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.graph import edges, nodes
from app.graph.checkpointer import SQLCheckpointSaver
from app.graph.deps import AgentDeps
from app.graph.state import AgentState

# Enough for the longest allowed path (two extra data loops and two rewrites), with
# room to spare; LangGraph stops a run that goes beyond it.
RECURSION_LIMIT = 50


def build_graph() -> StateGraph:
    graph = StateGraph(AgentState, context_schema=AgentDeps)
    graph.add_node("triage", nodes.triage_node)
    graph.add_node("supervisor", nodes.supervisor_node)
    graph.add_node("data_retrieval", nodes.data_retrieval_node)
    graph.add_node("knowledge", nodes.knowledge_node)
    graph.add_node("investigation", nodes.investigation_node)
    graph.add_node("human_review", nodes.human_review_node)
    graph.add_node("respond", nodes.respond_node)
    graph.add_node("validate", nodes.validate_node)
    graph.add_node("clarify", nodes.clarify_node)
    graph.add_node("finalize", nodes.finalize_node)

    graph.add_edge(START, "triage")
    graph.add_edge("triage", "supervisor")
    graph.add_conditional_edges("supervisor", edges.after_supervisor, edges.AFTER_SUPERVISOR)
    graph.add_edge("data_retrieval", "supervisor")
    graph.add_edge("knowledge", "supervisor")
    graph.add_conditional_edges(
        "investigation", edges.after_investigation, edges.AFTER_INVESTIGATION
    )
    graph.add_edge("human_review", "respond")
    graph.add_edge("respond", "validate")
    graph.add_conditional_edges("validate", edges.after_validation, edges.AFTER_VALIDATION)
    graph.add_edge("clarify", "finalize")
    graph.add_edge("finalize", END)
    return graph


def make_checkpointer(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession] | None
) -> BaseCheckpointSaver:
    """CHECKPOINTER=database (the default) saves to PostgreSQL; memory keeps it in RAM."""
    if settings.checkpointer == "database" and session_factory is not None:
        return SQLCheckpointSaver(session_factory)
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


def compile_graph(checkpointer: BaseCheckpointSaver | None = None) -> CompiledStateGraph:
    return build_graph().compile(checkpointer=checkpointer, name="orm_ai")


def draw_mermaid() -> str:
    return compile_graph().get_graph().draw_mermaid()
