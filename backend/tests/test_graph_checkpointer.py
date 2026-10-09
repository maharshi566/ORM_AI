"""The database checkpointer behaves like LangGraph's own in-memory one.

Each scenario runs twice, once on InMemorySaver (LangGraph's reference) and once on
SQLCheckpointSaver, and the results must match: loops, pausing with interrupt() and
resuming (what Phase 5 approvals rely on), state history, and deleting a workflow.
"""

import asyncio
import operator
import os
from pathlib import Path
from typing import Annotated, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.graph.checkpointer import SQLCheckpointSaver
from app.models import Base, GraphCheckpoint, GraphCheckpointBlob, GraphCheckpointWrite


class Counter(TypedDict, total=False):
    count: int
    log: Annotated[list[str], operator.add]
    approved: bool


def _looping_graph():
    def step(state: Counter) -> Counter:
        return {"count": state.get("count", 0) + 1, "log": [f"step {state.get('count', 0)}"]}

    def again(state: Counter) -> str:
        return "step" if state["count"] < 3 else END

    graph = StateGraph(Counter)
    graph.add_node("step", step)
    graph.add_edge(START, "step")
    graph.add_conditional_edges("step", again, ["step", END])
    return graph


def _approval_graph():
    def propose(state: Counter) -> Counter:
        return {"log": ["proposed"]}

    def ask(state: Counter) -> Counter:
        answer = interrupt({"question": "Send the reminder?"})
        return {"approved": answer == "yes", "log": [f"answer {answer}"]}

    def act(state: Counter) -> Counter:
        return {"log": ["sent" if state["approved"] else "not sent"]}

    graph = StateGraph(Counter)
    graph.add_node("propose", propose)
    graph.add_node("ask", ask)
    graph.add_node("act", act)
    graph.add_edge(START, "propose")
    graph.add_edge("propose", "ask")
    graph.add_edge("ask", "act")
    graph.add_edge("act", END)
    return graph


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'graph.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(params=["memory", "database"])
def saver(request, session_factory):
    return InMemorySaver() if request.param == "memory" else SQLCheckpointSaver(session_factory)


def thread(name: str) -> dict:
    return {"configurable": {"thread_id": name}}


async def test_loops_and_state_round_trip(saver) -> None:
    graph = _looping_graph().compile(checkpointer=saver)

    result = await graph.ainvoke({"count": 0}, thread("loop"))
    state = await graph.aget_state(thread("loop"))
    history = [snapshot async for snapshot in graph.aget_state_history(thread("loop"))]

    assert result == {"count": 3, "log": ["step 0", "step 1", "step 2"]}
    assert state.values == result and state.next == ()
    assert len(history) == 5  # input, 3 steps, plus the start checkpoint
    assert [h.values.get("count") for h in history] == [3, 2, 1, 0, None]


async def test_interrupt_and_resume(saver) -> None:
    graph = _approval_graph().compile(checkpointer=saver)

    paused = await graph.ainvoke({"log": []}, thread("approval"))
    waiting = await graph.aget_state(thread("approval"))
    resumed = await graph.ainvoke(Command(resume="yes"), thread("approval"))

    assert paused["__interrupt__"][0].value == {"question": "Send the reminder?"}
    assert waiting.next == ("ask",)
    assert resumed["log"] == ["proposed", "answer yes", "sent"]


async def test_resume_survives_a_new_saver_object(session_factory) -> None:
    """A restart: the paused workflow is read back from the database, not from memory."""
    first = _approval_graph().compile(checkpointer=SQLCheckpointSaver(session_factory))
    await first.ainvoke({"log": []}, thread("restart"))

    second = _approval_graph().compile(checkpointer=SQLCheckpointSaver(session_factory))
    resumed = await second.ainvoke(Command(resume="no"), thread("restart"))

    assert resumed["log"] == ["proposed", "answer no", "not sent"]


async def test_threads_are_separate_and_can_be_deleted(session_factory) -> None:
    saver = SQLCheckpointSaver(session_factory)
    graph = _looping_graph().compile(checkpointer=saver)
    await graph.ainvoke({"count": 1}, thread("a"))
    await graph.ainvoke({"count": 2}, thread("b"))

    await saver.adelete_thread("a")

    assert (await graph.aget_state(thread("a"))).values == {}
    assert (await graph.aget_state(thread("b"))).values["count"] == 3
    async with session_factory() as session:
        for table in (GraphCheckpoint, GraphCheckpointBlob, GraphCheckpointWrite):
            left = await session.scalar(
                select(func.count()).select_from(table).where(table.thread_id == "a")
            )
            assert left == 0


async def test_list_filters_and_limits_like_the_reference(session_factory) -> None:
    async def queries(saver) -> dict[str, list[int | None]]:
        graph = _looping_graph().compile(checkpointer=saver)
        await graph.ainvoke({"count": 0}, thread("list"))
        everything = [c async for c in saver.alist(thread("list"))]

        def counts(items) -> list[int | None]:
            return [c.checkpoint["channel_values"].get("count") for c in items]

        return {
            "all": counts(everything),
            "two": counts([c async for c in saver.alist(thread("list"), limit=2)]),
            "loop": counts(
                [c async for c in saver.alist(thread("list"), filter={"source": "loop"})]
            ),
            "older": counts(
                [c async for c in saver.alist(thread("list"), before=everything[1].config)]
            ),
        }

    expected = await queries(InMemorySaver())
    actual = await queries(SQLCheckpointSaver(session_factory))

    assert actual == expected
    assert expected["two"] == expected["all"][:2]  # newest first


async def test_a_failed_step_resumes_without_redoing_finished_work(saver) -> None:
    """Two steps run side by side; one fails. Retrying runs only the failed one."""
    runs: list[str] = []
    flaky = {"fail": True}
    stock_done = asyncio.Event()

    async def fetch_stock(state: Counter) -> Counter:
        runs.append("stock")
        stock_done.set()
        return {"log": ["stock"]}

    async def fetch_policy(state: Counter) -> Counter:
        runs.append("policy")
        await stock_done.wait()  # fail only after the other step has finished
        if flaky["fail"]:
            raise ConnectionError("vector store unavailable")
        return {"log": ["policy"]}

    builder = StateGraph(Counter)
    builder.add_node("fetch_stock", fetch_stock)
    builder.add_node("fetch_policy", fetch_policy)
    builder.add_edge(START, "fetch_stock")
    builder.add_edge(START, "fetch_policy")
    builder.add_edge(["fetch_stock", "fetch_policy"], END)
    graph = builder.compile(checkpointer=saver)

    with pytest.raises(ConnectionError):
        await graph.ainvoke({"log": []}, thread("flaky"))
    flaky["fail"] = False
    result = await graph.ainvoke(None, thread("flaky"))

    assert runs.count("stock") == 1  # finished work was kept, not done again
    assert runs.count("policy") == 2
    assert sorted(result["log"]) == ["policy", "stock"]


# ------------------------------------------------------------- on PostgreSQL

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
GRAPH_TABLES = [
    Base.metadata.tables[name]
    for name in ("graph_checkpoints", "graph_checkpoint_blobs", "graph_checkpoint_writes")
]


@pytest.fixture
async def postgres_sessions():
    """The checkpoint tables in a schema of their own, so the migration test is unaffected."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    admin = create_async_engine(TEST_DATABASE_URL)
    async with admin.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS checkpointer_test CASCADE"))
        await conn.execute(text("CREATE SCHEMA checkpointer_test"))
    engine = create_async_engine(
        TEST_DATABASE_URL, connect_args={"server_settings": {"search_path": "checkpointer_test"}}
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=GRAPH_TABLES))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
    async with admin.begin() as conn:
        await conn.execute(text("DROP SCHEMA checkpointer_test CASCADE"))
    await admin.dispose()


async def test_pause_and_resume_on_postgresql(postgres_sessions) -> None:
    """The same approval scenario on the real database driver (asyncpg, bytea)."""
    loop = _looping_graph().compile(checkpointer=SQLCheckpointSaver(postgres_sessions))
    assert (await loop.ainvoke({"count": 0}, thread("pg-loop")))["count"] == 3

    first = _approval_graph().compile(checkpointer=SQLCheckpointSaver(postgres_sessions))
    await first.ainvoke({"log": []}, thread("pg-approval"))
    second = _approval_graph().compile(checkpointer=SQLCheckpointSaver(postgres_sessions))
    resumed = await second.ainvoke(Command(resume="yes"), thread("pg-approval"))

    assert resumed["log"] == ["proposed", "answer yes", "sent"]
