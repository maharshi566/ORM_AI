"""POST and GET /api/approval/{workflow_id}: decide, record, resume.

The app runs with the seeded SQLite database, the real knowledge base, the scripted
model and the database checkpointer, so a "restart" (a new runtime with a new graph
and saver) is just building the runtime again.
"""

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select

from app.config.settings import Settings
from app.graph.checkpointer import SQLCheckpointSaver
from app.graph.workflow import compile_graph
from app.main import create_app
from app.models import Approval, AuditLog, Message, Notification, Workflow
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
LATE_PO = "Purchase order PO-00585 still has not arrived. What should I do?"
PRICE = "PRD-0085 is priced below cost. Please change the price to Rs 355."


@pytest.fixture
def make_runtime(session_factory, registry, knowledge):
    def build(*, memory_checkpointer: bool = False) -> AgentRuntime:
        settings = Settings(_env_file=None, **SETTINGS)
        saver = InMemorySaver() if memory_checkpointer else SQLCheckpointSaver(session_factory)
        return AgentRuntime(
            settings=settings,
            llm=RuleBasedLLM(),
            registry=registry,
            graph=compile_graph(saver),
            memory=ConversationMemory(session_factory, None),
            session_factory=session_factory,
            clients={"supplier_api": MockSupplierAPI(), "messaging_api": MockMessagingAPI()},
            knowledge=knowledge,
        )

    return build


@pytest.fixture
def api(make_runtime):
    app = create_app(Settings(_env_file=None, **SETTINGS))
    with TestClient(app) as client:
        app.state.agent_runtime = make_runtime()
        yield client


def ask(client: TestClient, message: str, shop_id: str) -> dict:
    response = client.post("/api/chat", json={"shop_id": shop_id, "message": message})
    assert response.status_code == 200, response.text
    return response.json()


async def test_approve_records_the_decision_then_resumes_and_acts(api, session_factory) -> None:
    paused = ask(api, LATE_PO, "SHOP-002")
    workflow_id = paused["workflow_id"]
    assert paused["status"] == "awaiting_approval"

    waiting = api.get(f"/api/approval/{workflow_id}").json()
    decided = api.post(
        f"/api/approval/{workflow_id}",
        json={"user_id": "USR-004", "decision": "approve", "note": "Yes, chase them"},
    )
    again = api.post(
        f"/api/approval/{workflow_id}", json={"user_id": "USR-004", "decision": "approve"}
    )

    assert waiting["workflow_status"] == "awaiting_approval"
    assert [a["status"] for a in waiting["approvals"]] == ["pending"]
    assert waiting["approval"]["actions"][0]["tool"] == "follow_up_supplier"
    body = decided.json()
    assert decided.status_code == 200, decided.text
    assert body["status"] == "completed" and body["validation"] == "PASS"
    [done] = body["proposed_actions"]
    assert done["status"] == "done" and done["result"]
    assert body["details"]["completed_actions"]
    assert again.status_code == 409  # already decided
    async with session_factory() as session:
        approval = (await session.scalars(select(Approval))).one()
        workflow = await session.get(Workflow, workflow_id)
        audit = (
            await session.scalars(
                select(AuditLog.action).where(AuditLog.workflow_id == workflow_id)
            )
        ).all()
        sent = await session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.purpose == "supplier_follow_up")
        )
        messages = (
            await session.scalars(
                select(Message.content).where(Message.session_id == paused["session_id"])
            )
        ).all()
    assert (approval.status, approval.decided_by) == ("approved", "USR-004")
    assert approval.decision_note == "Yes, chase them" and approval.final_action["tool"]
    assert {"approval_requested", "approval_approved", "follow_up_supplier"} <= set(audit)
    assert workflow.status == "completed" and workflow.completed_at is not None
    assert sent == 1
    assert len(messages) == 4  # question, "waiting" reply, the decision, the final reply


async def test_reject_records_the_decision_and_nothing_runs(api, session_factory) -> None:
    paused = ask(api, LATE_PO, "SHOP-002")

    body = api.post(
        f"/api/approval/{paused['workflow_id']}",
        json={"user_id": "USR-003", "decision": "reject", "note": "I will call them"},
    ).json()

    assert body["status"] == "completed"
    assert body["proposed_actions"][0]["status"] == "rejected"
    async with session_factory() as session:
        approval = (await session.scalars(select(Approval))).one()
    assert approval.status == "rejected" and approval.final_action is None


def test_only_the_owner_may_approve_what_needs_the_owner(api) -> None:
    paused = ask(api, PRICE, "SHOP-004")
    workflow_id = paused["workflow_id"]
    assert paused["approval"]["required_role"] == "owner"

    staff_yes = api.post(
        f"/api/approval/{workflow_id}", json={"user_id": "USR-008", "decision": "approve"}
    )
    owner_yes = api.post(
        f"/api/approval/{workflow_id}", json={"user_id": "USR-007", "decision": "approve"}
    )

    assert staff_yes.status_code == 403
    assert "Only the shop owner" in staff_yes.json()["error"]["message"]
    assert owner_yes.status_code == 200
    assert owner_yes.json()["proposed_actions"][0]["status"] == "done"


def test_a_change_is_checked_with_the_tools_own_rules(api) -> None:
    paused = ask(api, PRICE, "SHOP-004")
    workflow_id = paused["workflow_id"]
    [waiting] = paused["approval"]["actions"]

    def modify(arguments: dict) -> dict:
        return {
            "user_id": "USR-007",
            "decisions": [
                {
                    "approval_id": waiting["approval_id"],
                    "decision": "modify",
                    "arguments": arguments,
                }
            ],
        }

    bad = api.post(f"/api/approval/{workflow_id}", json=modify({"product_id": "PRD-0085"}))
    good = api.post(
        f"/api/approval/{workflow_id}",
        json=modify(
            {"product_id": "PRD-0085", "new_selling_price": 358, "reason": "Owner chose Rs 358."}
        ),
    )

    assert bad.status_code == 422 and "not valid" in bad.json()["error"]["message"]
    assert good.status_code == 200
    [changed] = good.json()["proposed_actions"]
    assert changed["status"] == "done" and changed["arguments"]["new_selling_price"] == "358"


def test_who_decides_and_what_is_decided_are_checked(api) -> None:
    paused = ask(api, LATE_PO, "SHOP-002")
    workflow_id = paused["workflow_id"]

    other_shop = api.post(
        f"/api/approval/{workflow_id}", json={"user_id": "USR-001", "decision": "approve"}
    )
    unknown = api.post(
        "/api/approval/no-such-workflow", json={"user_id": "USR-003", "decision": "reject"}
    )
    no_decision = api.post(f"/api/approval/{workflow_id}", json={"user_id": "USR-003"})
    stranger = api.post(
        f"/api/approval/{workflow_id}",
        json={
            "user_id": "USR-003",
            "decisions": [{"approval_id": "not-in-this-workflow", "decision": "approve"}],
        },
    )

    assert other_shop.status_code == 403
    assert unknown.status_code == 404
    assert no_decision.status_code == 422
    assert stranger.status_code == 422


def test_an_approval_survives_a_restart(api, make_runtime) -> None:
    paused = ask(api, LATE_PO, "SHOP-002")

    api.app.state.agent_runtime = make_runtime()  # a new process: new graph, new saver
    body = api.post(
        f"/api/approval/{paused['workflow_id']}", json={"user_id": "USR-004", "decision": "approve"}
    ).json()

    assert body["status"] == "completed"
    assert body["proposed_actions"][0]["status"] == "done"


def test_a_lost_in_memory_state_is_explained(api, make_runtime) -> None:
    api.app.state.agent_runtime = make_runtime(memory_checkpointer=True)
    paused = ask(api, LATE_PO, "SHOP-002")

    api.app.state.agent_runtime = make_runtime(memory_checkpointer=True)  # restart: state gone
    response = api.post(
        f"/api/approval/{paused['workflow_id']}", json={"user_id": "USR-004", "decision": "approve"}
    )

    assert response.status_code == 409
    assert "can no longer be resumed" in response.json()["error"]["message"]


def test_staff_cannot_change_an_action_into_one_for_the_owner(api) -> None:
    paused = ask(
        api,
        "I counted PRD-0002 and found 6 fewer than the system. Please record the adjustment.",
        "SHOP-001",
    )
    [waiting] = paused["approval"]["actions"]
    bigger = {
        "user_id": "USR-002",
        "decisions": [
            {
                "approval_id": waiting["approval_id"],
                "decision": "modify",
                "arguments": {
                    "product_id": "PRD-0002",
                    "quantity_change": -20,
                    "movement_type": "adjustment",
                    "reason": "Physical count was lower than the system.",
                },
            }
        ],
    }
    other_record = {
        "user_id": "USR-001",
        "decisions": [
            {
                "approval_id": waiting["approval_id"],
                "decision": "modify",
                "arguments": {**bigger["decisions"][0]["arguments"], "product_id": "PRD-0003"},
            }
        ],
    }

    staff = api.post(f"/api/approval/{paused['workflow_id']}", json=bigger)
    switched = api.post(f"/api/approval/{paused['workflow_id']}", json=other_record)

    assert staff.status_code == 403 and "owner" in staff.json()["error"]["message"]
    assert switched.status_code == 422 and "product_id" in switched.json()["error"]["message"]


async def test_a_resume_cut_off_by_a_crash_is_picked_up_again(
    api, session_factory, monkeypatch
) -> None:
    from datetime import timedelta

    from app.models.types import utcnow
    from app.services.approval_service import ApprovalService

    paused = ask(api, LATE_PO, "SHOP-002")
    workflow_id = paused["workflow_id"]
    original = ApprovalService._resume

    async def crash(*args, **kwargs):
        raise RuntimeError("the process died")

    monkeypatch.setattr(ApprovalService, "_resume", crash)
    with pytest.raises(RuntimeError):
        api.post(f"/api/approval/{workflow_id}", json={"user_id": "USR-004", "decision": "approve"})
    monkeypatch.setattr(ApprovalService, "_resume", original)

    too_soon = api.post(
        f"/api/approval/{workflow_id}", json={"user_id": "USR-004", "decision": "approve"}
    )
    async with session_factory() as session:
        workflow = await session.get(Workflow, workflow_id)
        workflow.updated_at = utcnow() - timedelta(minutes=5)
        await session.commit()
    later = api.post(
        f"/api/approval/{workflow_id}", json={"user_id": "USR-004", "decision": "reject"}
    )

    assert too_soon.status_code == 409  # it may still be running: not started twice
    assert later.status_code == 200
    # The recorded decision (approve) is used, not the new one (reject).
    assert later.json()["proposed_actions"][0]["status"] == "done"
