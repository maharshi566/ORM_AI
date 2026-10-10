"""The whole agent graph, end to end, with a scripted model.

Everything except the model is real: the tools run on the seeded database, the
knowledge agent searches the real knowledge base, and the validator checks the reply.
These tests check the paths through the graph (who runs, in what order, how often),
the loop limits, what happens when the model or a tool fails, and that nothing is
ever executed or claimed as done.
"""

from sqlalchemy import func, select

from app.agents.supervisor import OUT_OF_SCOPE_REPLY
from app.config.settings import Settings
from app.graph.checkpointer import SQLCheckpointSaver
from app.graph.workflow import compile_graph, draw_mermaid
from app.models import AgentRun, Notification, ToolCall, Workflow
from app.tools.api_tools import FailureMode, FaultInjector
from tests.fake_llm import RuleBasedLLM


def path(state: dict) -> list[str]:
    return [step["agent"] for step in state["agent_trace"]]


def tools_called(state: dict) -> list[str]:
    return [call["tool"] for call in state.get("tool_results") or []]


async def test_a_supplier_problem_goes_through_every_specialist_then_waits(
    make_deps, run_agent, session_factory
) -> None:
    llm = RuleBasedLLM()
    state = await run_agent(
        make_deps(llm),
        "Purchase order PO-00585 still has not arrived. What should I do?",
        shop_id="SHOP-002",
    )

    assert path(state) == [  # human_review paused, so it has no trace entry yet
        "triage",
        "supervisor",
        "data_retrieval",
        "supervisor",
        "knowledge",
        "supervisor",
        "investigation",
    ]
    assert state["intent"] == "supplier_issue" and not state.get("final_response")
    [interrupt] = state["__interrupt__"]
    request = interrupt.value
    [action] = request["actions"]
    assert action["tool"] == "follow_up_supplier" and action["required_role"] == "staff"
    assert action["arguments"] == {
        "purchase_order_id": "PO-00585",
        "issue": "late",
        "channel": "whatsapp",
    }
    assert any(c.startswith("[POL-SUPPLIER-001") for c in request["policy_references"])
    assert "Nothing has been changed yet" in request["message"]
    [proposed] = state["proposed_actions"]
    assert proposed["arguments"]["idempotency_key"] == (
        f"{state['workflow_id']}:follow_up_supplier:1"
    )
    # Proposing is not doing: no action tool ran and no message was sent.
    assert {c["agent"] for c in state["tool_results"]} == {"data_retrieval", "knowledge"}
    async with session_factory() as session:
        sent = await session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.purpose == "supplier_follow_up")
        )
    assert sent == 0
    assert llm.calls == ["triage", "data_retrieval", "data_retrieval", "investigation"]


async def test_a_sales_question_needs_no_rules_and_no_investigation(make_deps, run_agent) -> None:
    state = await run_agent(make_deps(), "What were my total sales last week?", shop_id="SHOP-005")

    assert path(state) == [
        "triage",
        "supervisor",
        "data_retrieval",
        "supervisor",
        "respond",
        "validate",
        "finalize",
    ]
    assert tools_called(state) == ["get_sales_summary"]
    [record] = state["retrieved_data"].values()
    assert record["arguments"] == {"start_date": "2026-09-24", "end_date": "2026-09-30"}


async def test_a_policy_question_only_searches_the_rules(make_deps, run_agent) -> None:
    state = await run_agent(make_deps(), "What is the credit limit for a household?")

    assert "data_retrieval" not in path(state)
    assert set(tools_called(state)) == {"search_knowledge"}
    assert any("POL-CREDIT-001 v2" in s["citation"] for s in state["sources"])


async def test_out_of_scope_gets_a_fixed_reply_without_tools(make_deps, run_agent) -> None:
    llm = RuleBasedLLM()
    state = await run_agent(make_deps(llm), "Who won the cricket match yesterday?")

    assert state["final_response"] == OUT_OF_SCOPE_REPLY
    assert path(state) == ["triage", "supervisor", "finalize"]
    assert llm.calls == ["triage"] and not state.get("tool_results")


async def test_an_unclear_request_gets_a_question_back(make_deps, run_agent) -> None:
    state = await run_agent(make_deps(), "Send a payment reminder please")

    assert state["outcome"] == "needs_clarification"
    assert state["final_response"] == "Which customer should I remind?"
    assert path(state) == ["triage", "supervisor", "clarify", "finalize"]


async def test_the_investigation_can_ask_for_more_data(make_deps, run_agent) -> None:
    state = await run_agent(
        make_deps(RuleBasedLLM(more_data=1)),
        "Counted PRD-0002 today and found 6 fewer than the system shows. What should I do?",
    )

    steps = path(state)
    assert steps.count("investigation") == 2 and steps.count("data_retrieval") == 2
    assert state["retrieval_loops"] == 1
    assert steps.index("investigation") < len(steps) - 1 - steps[::-1].index("data_retrieval")


async def test_the_more_data_loop_is_capped(make_deps, run_agent) -> None:
    state = await run_agent(
        make_deps(RuleBasedLLM(more_data=10), agent_max_loops=2),
        "Counted PRD-0002 today and found 6 fewer than the system shows. What should I do?",
    )

    assert path(state).count("investigation") == 3  # the first look plus two loops
    assert state["retrieval_loops"] == 2
    assert state["final_response"]


async def test_a_made_up_citation_is_sent_back_and_rewritten(make_deps, run_agent) -> None:
    state = await run_agent(
        make_deps(RuleBasedLLM(bad_citations=1)), "What is the credit limit for a household?"
    )

    assert path(state).count("respond") == 2 and state["response_retries"] == 1
    assert state["validation_result"] == "PASS"
    assert "POL-MADEUP-999" not in state["final_response"]


async def test_after_two_rewrites_the_reply_goes_out_marked_unverified(
    make_deps, run_agent
) -> None:
    state = await run_agent(
        make_deps(RuleBasedLLM(bad_citations=10)), "What is the credit limit for a household?"
    )

    assert path(state).count("respond") == 3
    assert state["validation_result"] == "HUMAN_REVIEW"
    assert "POL-MADEUP-999" not in state["final_response"]  # removed from the reply
    assert "could not be fully checked" in state["final_response"]


async def test_model_down_at_triage_fails_clearly_without_touching_records(
    make_deps, run_agent
) -> None:
    state = await run_agent(
        make_deps(RuleBasedLLM(fail=["triage"])), "How much does CUST-0001 owe?"
    )

    assert state["outcome"] == "failed"
    assert "The fake model is down for triage" in state["final_response"]
    assert path(state) == ["triage", "supervisor", "finalize"]
    assert not state.get("tool_results")


async def test_model_down_at_the_reply_still_reports_what_was_found(make_deps, run_agent) -> None:
    state = await run_agent(
        make_deps(RuleBasedLLM(fail=["respond"])), "How much does CUST-0001 owe?"
    )

    assert state["outcome"] == "failed"
    assert "get_customer_account(customer_id=CUST-0001)" in state["final_response"]
    assert "respond" in state["failed_agents"]


async def test_a_model_without_tool_calling_falls_back_to_a_fixed_plan(
    make_deps, run_agent
) -> None:
    state = await run_agent(make_deps(RuleBasedLLM(no_tools=True)), "How much does CUST-0001 owe?")

    assert tools_called(state)[0] == "get_customer_account"
    assert any("did not call any tools" in w for w in state["warnings"])
    assert state["outcome"] == "completed"


async def test_a_failing_supplier_api_is_reported_not_hidden(make_deps, run_agent) -> None:
    state = await run_agent(
        make_deps(faults=FaultInjector(FailureMode.SERVER_ERROR)),
        "The margin on PRD-0085 looks wrong. Is it priced correctly?",
        shop_id="SHOP-004",
    )

    price = next(c for c in state["tool_results"] if c["tool"] == "check_supplier_price")
    assert price["status"] == "error" and price["error_code"] == "upstream_error"
    assert "check_supplier_price failed (upstream_error)" in state["errors"]
    assert state["final_response"]  # still answered from the records it has


async def test_tools_only_see_the_asking_shop(make_deps, run_agent) -> None:
    """SHOP-001 asks about SHOP-002's order: the tools never return it."""
    state = await run_agent(
        make_deps(), "Purchase order PO-00585 still has not arrived. What should I do?"
    )

    fetched = str(state["retrieved_data"])
    assert "PO-00585" not in fetched
    assert all("SHOP-002" not in str(r) for r in state["retrieved_data"].values())


async def test_every_step_is_checkpointed_in_the_database(
    make_deps, run_agent, session_factory
) -> None:
    saver = SQLCheckpointSaver(session_factory)
    state = await run_agent(
        make_deps(), "How much does CUST-0001 owe?", checkpointer=saver, workflow_id="wf-check"
    )

    thread = {"configurable": {"thread_id": "wf-check"}}
    saved = await compile_graph(saver).aget_state(thread)
    assert saved.values["final_response"] == state["final_response"]
    history = [s async for s in compile_graph(saver).aget_state_history(thread)]
    assert len(history) > len(state["agent_trace"])  # one checkpoint per step, and the input


async def test_agent_runs_and_tool_calls_are_recorded(
    make_deps, run_agent, session_factory
) -> None:
    async with session_factory() as session:
        session.add(Workflow(id="wf-log", shop_id="SHOP-001", user_query="owe?", status="running"))
        await session.commit()

    state = await run_agent(
        make_deps(record_to_db=True), "How much does CUST-0001 owe?", workflow_id="wf-log"
    )

    async with session_factory() as session:
        runs = (
            await session.scalars(select(AgentRun).where(AgentRun.workflow_id == "wf-log"))
        ).all()
        calls = (
            await session.scalars(select(ToolCall).where(ToolCall.workflow_id == "wf-log"))
        ).all()
    assert [r.agent for r in runs] == path(state)
    triage = next(r for r in runs if r.agent == "triage")
    assert (triage.model, triage.input_tokens, triage.output_tokens) == ("fake-model", 100, 20)
    assert {c.tool_name for c in calls} == {"get_customer_account", "search_knowledge"}


def test_the_graph_matches_the_design() -> None:
    mermaid = draw_mermaid()

    for node in (
        "triage",
        "supervisor",
        "data_retrieval",
        "knowledge",
        "investigation",
        "human_review",
        "action",
        "respond",
        "validate",
        "clarify",
        "finalize",
    ):
        assert f"\t{node}(" in mermaid
    for edge in (
        "__start__ --> triage",
        "triage --> supervisor",
        "data_retrieval --> supervisor",
        "knowledge --> supervisor",
        "supervisor -.-> investigation",
        "investigation -.-> data_retrieval",  # need more data
        "investigation -.-> human_review",
        "investigation -.-> respond",
        "human_review -.-> action",  # approved: carry it out
        "human_review -.-> respond",  # rejected or nothing to run
        "action --> respond",
        "respond --> validate",
        "validate -.-> respond",  # RETRY
        "validate -.-> finalize",
        "finalize --> __end__",
    ):
        assert edge in mermaid, edge


async def test_the_graph_runs_through_the_real_model_client(make_deps, run_agent) -> None:
    """Same flow, but every model call goes through OpenAIChatModel and the OpenAI SDK.

    The scripted gateway also rejects any schema that OpenAI's strict mode would
    reject, so this catches schema problems before a real model sees them.
    """
    from app.services.chat_model import OpenAIChatModel
    from tests.fake_gateway import BASE_URL
    from tests.scripted_gateway import ScriptedGateway

    gateway = ScriptedGateway()
    deps = make_deps()
    deps.llm = OpenAIChatModel(
        Settings(
            _env_file=None,
            llm_base_url=BASE_URL,
            llm_api_key="sk-test",
            llm_model_fast="fake-fast",
            llm_model_smart="fake-smart",
            llm_max_retries=0,
        ),
        http_client=gateway.client(),
    )

    state = await run_agent(
        deps,
        "Purchase order PO-00585 still has not arrived. What should I do?",
        shop_id="SHOP-002",
        decision="reject",
    )

    assert state["validation_result"] == "PASS", state.get("errors")
    assert state["proposed_actions"][0]["status"] == "rejected"
    assert state["proposed_actions"][0]["tool"] == "follow_up_supplier"
    formats = [r["response_format"]["type"] for r in gateway.requests if "response_format" in r]
    assert formats == ["json_schema"] * 3  # triage, investigation, reply: no downgrade
    tool_rounds = [r for r in gateway.requests if r.get("tools")]
    assert tool_rounds[-1]["messages"][-1]["role"] == "tool"  # results went back to the model
    triage = state["agent_trace"][0]
    assert (triage["model"], triage["input_tokens"]) == ("fake-fast", 50)


def test_the_graph_document_is_up_to_date() -> None:
    from scripts.draw_graph import DOC, render

    assert DOC.read_text(encoding="utf-8") == render(), "run: python -m scripts.draw_graph --write"
