"""Phase 7: the read-only lists the website needs, and names for the logged-in user.

``GET /api/auth/dev-users`` (the login page), ``GET /api/sessions`` (recent
conversations), ``GET /api/workflows`` (recent requests), ``GET /api/approvals`` (the
approvals inbox) and ``GET /api/evaluations`` (stored evaluation runs). Each list
stays inside the caller's shop; evaluations are for admins.
"""

from app.agents.evaluation import (
    AgentCase,
    AgentEvalReport,
    CaseOutcome,
    evaluation_rows,
    save_report,
)
from tests.test_api_phase6 import LATE_PO, api, make_client, token_for  # noqa: F401

CREDIT = "How much does CUST-0001 owe?"


# ------------------------------------------------------------------ logins


async def test_the_login_page_lists_demo_users_with_their_shops(api) -> None:  # noqa: F811
    users = (await api.get("/api/auth/dev-users")).json()

    first = next(u for u in users if u["user_id"] == "USR-001")
    admin = next(u for u in users if u["role"] == "admin")
    assert first["shop_id"] == "SHOP-001" and first["role"] == "owner"
    assert first["name"] and first["shop_name"]
    assert admin["shop_id"] is None and users[-1]["role"] == "admin"  # shopless last


async def test_the_demo_user_list_follows_the_dev_login_rule(make_client) -> None:  # noqa: F811
    production = make_client(app_env="production")
    switched_off = make_client(auth_dev_login=False)

    assert (await production.get("/api/auth/dev-users")).status_code == 403
    assert (await switched_off.get("/api/auth/dev-users")).status_code == 403


async def test_a_login_carries_the_users_and_shops_names(api) -> None:  # noqa: F811
    login = (await api.post("/api/auth/dev-token", json={"user_id": "USR-002"})).json()
    headers = {"Authorization": f"Bearer {login['access_token']}"}

    me = (await api.get("/api/auth/me", headers=headers)).json()

    assert login["role"] == "staff" and login["name"] and login["shop_name"]
    assert (me["name"], me["shop_name"]) == (login["name"], login["shop_name"])


async def test_shop_users_see_their_shop_and_admins_every_shop(api) -> None:  # noqa: F811
    own = (await api.get("/api/shops", headers=await token_for(api, "USR-003"))).json()
    every = (await api.get("/api/shops", headers=await token_for(api, "USR-101"))).json()

    assert [s["shop_id"] for s in own["shops"]] == ["SHOP-002"]
    assert own["shops"][0]["name"] and own["shops"][0]["city"]
    assert len(every["shops"]) == 50 and every["shops"][0]["shop_id"] == "SHOP-001"


# ------------------------------------------------------------------ conversations


async def test_a_user_sees_their_own_recent_conversations(api) -> None:  # noqa: F811
    owner = await token_for(api, "USR-001")
    staff = await token_for(api, "USR-002")
    first = (
        await api.post("/api/chat", json={"shop_id": "SHOP-001", "message": CREDIT}, headers=owner)
    ).json()
    await api.post(
        "/api/chat",
        json={"shop_id": "SHOP-001", "message": CREDIT, "session_id": first["session_id"]},
        headers=owner,
    )
    await api.post("/api/chat", json={"shop_id": "SHOP-001", "message": CREDIT}, headers=staff)

    mine = (await api.get("/api/sessions", headers=owner)).json()["sessions"]
    everyone = (await api.get("/api/sessions", headers=await token_for(api, "USR-101"))).json()

    assert [s["session_id"] for s in mine] == [first["session_id"]]
    assert mine[0]["workflows"] == 2 and mine[0]["last_status"] == "completed"
    assert len(everyone["sessions"]) == 2


# ------------------------------------------------------------------ workflows


async def test_recent_workflows_newest_first_inside_the_shop(api) -> None:  # noqa: F811
    await api.post("/api/chat", json={"shop_id": "SHOP-001", "message": CREDIT})
    await api.post("/api/chat", json={"shop_id": "SHOP-002", "message": LATE_PO})
    owner = await token_for(api, "USR-001")

    everything = (await api.get("/api/workflows")).json()["workflows"]
    waiting = (await api.get("/api/workflows?status=awaiting_approval")).json()["workflows"]
    own = (await api.get("/api/workflows", headers=owner)).json()["workflows"]
    other = await api.get("/api/workflows?shop_id=SHOP-002", headers=owner)
    bad = await api.get("/api/workflows?status=sleeping")

    assert [w["shop_id"] for w in everything] == ["SHOP-002", "SHOP-001"]
    assert [w["shop_id"] for w in waiting] == ["SHOP-002"]
    assert [w["shop_id"] for w in own] == ["SHOP-001"]
    assert other.status_code == 403
    assert bad.status_code == 422


# ------------------------------------------------------------------ approvals inbox


async def test_the_inbox_lists_what_waits_with_its_request(api) -> None:  # noqa: F811
    chat = (await api.post("/api/chat", json={"shop_id": "SHOP-002", "message": LATE_PO})).json()

    inbox = (await api.get("/api/approvals?status=pending")).json()
    shop_002 = (await api.get("/api/approvals", headers=await token_for(api, "USR-004"))).json()
    shop_001 = (await api.get("/api/approvals", headers=await token_for(api, "USR-001"))).json()

    item = inbox["approvals"][0]
    assert item["workflow_id"] == chat["workflow_id"]
    assert item["tool"] == chat["approval"]["actions"][0]["tool"]
    assert item["description"].startswith(item["tool"] + " (")
    assert "idempotency_key" not in item["arguments"]
    assert item["user_query"] == LATE_PO and item["workflow_status"] == "awaiting_approval"
    assert item["status"] == "pending" and item["required_role"] in {"staff", "owner"}
    assert inbox["counts"]["pending"] == 1
    assert len(shop_002["approvals"]) == 1
    assert shop_001["approvals"] == [] and shop_001["counts"]["pending"] == 0


async def test_a_decided_approval_moves_out_of_the_pending_list(api) -> None:  # noqa: F811
    chat = (await api.post("/api/chat", json={"shop_id": "SHOP-002", "message": LATE_PO})).json()
    await api.post(
        f"/api/approval/{chat['workflow_id']}",
        json={"user_id": "USR-003", "decision": "reject", "note": "I will call them"},
    )

    pending = (await api.get("/api/approvals?status=pending")).json()
    rejected = (await api.get("/api/approvals?status=rejected")).json()["approvals"]

    assert pending["approvals"] == [] and pending["counts"]["rejected"] == 1
    assert rejected[0]["decided_by"] == "USR-003"
    assert rejected[0]["decision_note"] == "I will call them"
    assert rejected[0]["workflow_status"] == "completed"


# ------------------------------------------------------------------ evaluations


def _report() -> AgentEvalReport:
    normal = AgentCase(id="A01", shop_id="SHOP-001", message="Low stock?", intents=("reorder",))
    approval = AgentCase(
        id="H01",
        shop_id="SHOP-002",
        message="Message the supplier",
        intents=("supplier_issue",),
        approval_tool="follow_up_supplier",
        approval_role="staff",
    )

    def outcome(case: AgentCase, intent: str, **extra) -> CaseOutcome:
        return CaseOutcome(
            case=case,
            intent=intent,
            missing_records=[],
            cited_documents=["POL-REORDER-001"],
            validation="PASS",
            latency_ms=1200.0,
            input_tokens=900,
            output_tokens=120,
            answer="An answer.",
            **extra,
        )

    return AgentEvalReport(
        outcomes=[
            outcome(normal, "reorder"),
            outcome(
                approval,
                "stock_status",
                asked=[("follow_up_supplier", "staff")],
                outcome="completed",
            ),
        ],
        models="fast=small, smart=large",
    )


def test_each_case_becomes_one_row_with_its_checks() -> None:
    rows = evaluation_rows(_report(), "run-1")

    assert [(r["case_id"], r["category"], r["passed"]) for r in rows] == [
        ("A01", "normal", True),
        ("H01", "approval", False),
    ]
    assert rows[1]["scores"]["intent"] == 0.0 and rows[1]["scores"]["approval"] == 1.0
    assert "approval" not in rows[0]["scores"]
    assert rows[0]["details"]["models"] == "fast=small, smart=large"


async def test_admins_see_stored_evaluation_runs(api, session_factory) -> None:  # noqa: F811
    run_id = await save_report(session_factory, _report())

    as_admin = await api.get("/api/evaluations", headers=await token_for(api, "USR-101"))
    as_owner = await api.get("/api/evaluations", headers=await token_for(api, "USR-001"))

    run = as_admin.json()["runs"][0]
    assert run["run_id"] == run_id and run["cases"] == 2 and run["passed"] == 1
    assert run["scores"]["intent"] == 0.5 and run["scores"]["approval"] == 1.0
    assert run["models"] == "fast=small, smart=large"
    assert [r["case_id"] for r in run["results"]] == ["A01", "H01"]
    assert as_owner.status_code == 403


# ------------------------------------------------------------------ old replies


async def test_a_workflow_gives_back_the_reply_the_chat_showed(api) -> None:  # noqa: F811
    paused = (await api.post("/api/chat", json={"shop_id": "SHOP-002", "message": LATE_PO})).json()
    before = (await api.get(f"/api/workflows/{paused['workflow_id']}")).json()["reply"]
    decided = (
        await api.post(
            f"/api/approval/{paused['workflow_id']}",
            json={"user_id": "USR-003", "decision": "approve"},
        )
    ).json()
    after = (await api.get(f"/api/workflows/{paused['workflow_id']}")).json()["reply"]

    assert before["status"] == "awaiting_approval"
    assert before["answer"] == paused["answer"] and before["sources"] == paused["sources"]
    assert before["approval"]["actions"] == paused["approval"]["actions"]
    assert after["status"] == decided["status"] == "completed"
    assert after["answer"] == decided["answer"]
    assert [a["status"] for a in after["proposed_actions"]] == [
        a["status"] for a in decided["proposed_actions"]
    ]


async def test_a_workflow_without_saved_state_has_no_reply(api, session_factory) -> None:  # noqa: F811
    from app.models import Workflow

    async with session_factory() as db:
        db.add(Workflow(id="wf-gone", shop_id="SHOP-001", user_query="?", status="completed"))
        await db.commit()

    view = (await api.get("/api/workflows/wf-gone")).json()

    assert view["status"] == "completed" and view["reply"] is None


async def test_a_workflow_still_running_has_no_reply_yet(api, session_factory) -> None:  # noqa: F811
    from app.models import Workflow

    chat = (await api.post("/api/chat", json={"shop_id": "SHOP-001", "message": CREDIT})).json()
    async with session_factory() as db:
        row = await db.get(Workflow, chat["workflow_id"])
        row.status = "running"  # as while a decision is being carried out
        await db.commit()

    view = (await api.get(f"/api/workflows/{chat['workflow_id']}")).json()

    assert view["status"] == "running" and view["reply"] is None
