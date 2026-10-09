"""POST /api/chat and GET /api/chat/graph.

The app runs with a prepared AgentRuntime: the seeded SQLite database, the real
knowledge base, the scripted model and an in-memory checkpointer. Everything from the
HTTP request to the saved workflow row is real.
"""

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app.config.settings import Settings
from app.graph.workflow import compile_graph
from app.main import create_app
from app.models import AgentRun, ChatSession, Message, Workflow
from app.services.chat_service import AgentRuntime
from app.services.memory_service import ConversationMemory
from app.tools.api_tools import MockMessagingAPI, MockSupplierAPI
from tests.fake_llm import RuleBasedLLM

SETTINGS = {
    "app_env": "test",
    "business_date": "2026-09-30",
    "database_url": "postgresql+asyncpg://test:test@localhost:5432/orm_ai_test",
    "redis_url": "redis://localhost:6379/15",
}


@pytest.fixture
def chat_client(session_factory, registry, knowledge):
    settings = Settings(_env_file=None, **SETTINGS)
    app = create_app(settings)
    runtime = AgentRuntime(
        settings=settings,
        llm=RuleBasedLLM(),
        registry=registry,
        graph=compile_graph(InMemorySaver()),
        memory=ConversationMemory(session_factory, None),
        session_factory=session_factory,
        clients={"supplier_api": MockSupplierAPI(), "messaging_api": MockMessagingAPI()},
        knowledge=knowledge,
    )
    with TestClient(app) as client:
        app.state.agent_runtime = runtime
        yield client


def ask(client: TestClient, message: str, **extra) -> dict:
    response = client.post("/api/chat", json={"shop_id": "SHOP-001", "message": message, **extra})
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_chat_returns_a_cited_answer_and_records_the_workflow(
    chat_client, session_factory
) -> None:
    body = ask(chat_client, "How much does CUST-0001 owe? Can I give them more credit?")

    assert body["status"] == "completed" and body["intent"] == "customer_credit"
    assert body["validation"] == "PASS"
    assert body["sources"] and body["sources"][0]["citation"] in body["answer"]
    assert {c["tool"] for c in body["tool_calls"]} == {"get_customer_account", "search_knowledge"}
    assert [a["agent"] for a in body["agents"]][0] == "triage"
    # triage, two tool rounds, investigation and the reply, 100 input tokens each (fake)
    assert body["usage"]["llm_calls"] == 5 and body["usage"]["input_tokens"] == 500
    async with session_factory() as session:
        workflow = await session.get(Workflow, body["workflow_id"])
        messages = (
            await session.scalars(select(Message).where(Message.session_id == body["session_id"]))
        ).all()
        runs = (
            await session.scalars(
                select(AgentRun).where(AgentRun.workflow_id == body["workflow_id"])
            )
        ).all()
    assert workflow.status == "completed" and workflow.intent == "customer_credit"
    assert workflow.final_response == body["answer"] and workflow.completed_at is not None
    assert [str(m.role) for m in messages] == ["user", "assistant"]
    assert len(runs) == len(body["agents"])


def test_a_conversation_continues_with_its_session_id(chat_client) -> None:
    first = ask(chat_client, "How much does CUST-0001 owe?")
    second = ask(
        chat_client, "What is the credit limit for a household?", session_id=first["session_id"]
    )

    assert second["session_id"] == first["session_id"]
    assert second["workflow_id"] != first["workflow_id"]


def test_proposed_actions_are_shown_but_not_done(chat_client) -> None:
    response = chat_client.post(
        "/api/chat",
        json={
            "shop_id": "SHOP-002",
            "message": "Purchase order PO-00585 still has not arrived. What should I do?",
        },
    )

    body = response.json()
    [action] = body["proposed_actions"]
    assert action["tool"] == "follow_up_supplier" and action["status"] == "awaiting_approval"
    assert "idempotency_key" not in action["arguments"]
    assert body["details"]["completed_actions"] == []
    assert body["details"]["pending_approval"]


def test_unknown_shops_users_and_other_shops_conversations_are_refused(
    chat_client, session_factory
) -> None:
    other = chat_client.post(
        "/api/chat", json={"shop_id": "SHOP-002", "message": "Hello, what can you do?"}
    ).json()

    no_shop = chat_client.post("/api/chat", json={"shop_id": "SHOP-999", "message": "hi"})
    wrong_user = chat_client.post(
        "/api/chat", json={"shop_id": "SHOP-001", "message": "hi", "user_id": "USR-004"}
    )
    stolen = chat_client.post(
        "/api/chat",
        json={"shop_id": "SHOP-001", "message": "hi", "session_id": other["session_id"]},
    )

    assert no_shop.status_code == 404
    assert wrong_user.status_code == 403 and wrong_user.json()["error"]["code"] == "forbidden"
    assert stolen.status_code == 404  # another shop's conversation looks like no conversation


def test_bad_requests_are_rejected_before_any_agent_runs(chat_client) -> None:
    empty = chat_client.post("/api/chat", json={"shop_id": "SHOP-001", "message": ""})
    bad_shop = chat_client.post("/api/chat", json={"shop_id": "shop 1", "message": "hi"})
    too_long = chat_client.post("/api/chat", json={"shop_id": "SHOP-001", "message": "x" * 2001})

    assert {empty.status_code, bad_shop.status_code, too_long.status_code} == {422}


def test_without_a_model_the_chat_explains_what_to_set() -> None:
    app = create_app(Settings(_env_file=None, **SETTINGS))
    with TestClient(app) as client:
        response = client.post("/api/chat", json={"shop_id": "SHOP-001", "message": "hi"})

    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["error"]["message"]


def test_the_graph_endpoint_returns_mermaid(chat_client) -> None:
    mermaid = chat_client.get("/api/chat/graph").json()["mermaid"]

    assert "supervisor -.-> data_retrieval" in mermaid


async def test_new_conversations_belong_to_the_asking_shop(chat_client, session_factory) -> None:
    body = ask(chat_client, "Hello, what can you do?")

    async with session_factory() as session:
        conversation = await session.get(ChatSession, body["session_id"])
    assert conversation.shop_id == "SHOP-001"
