"""The rules inside the agents, one at a time, without running the graph."""

import json

import pytest

from app.agents.common import citations_in, ids_in, record_key
from app.agents.investigation import check_actions, check_result
from app.agents.retrieval import fallback_calls
from app.agents.schemas import (
    Evidence,
    InvestigationResult,
    ProposedAction,
    empty_entities,
)
from app.agents.supervisor import make_plan, next_step
from app.agents.triage import ground_entities
from app.agents.validator import find_problems
from app.graph.nodes import render_reply
from app.graph.state import merge_passages, merge_records

CREDIT = "[POL-CREDIT-001 v2 §2. Credit limits]"


def triage(intent: str, **extra) -> dict:
    return {"intent": intent, "recommended_route": [], "wants_action": False, **extra}


# ------------------------------------------------------------------- triage


def test_ids_are_kept_only_when_the_conversation_contains_them() -> None:
    entities = empty_entities()
    entities.customer_ids = ["cust-0004", "CUST-9999"]  # one real, one invented
    entities.product_ids = []

    grounded = ground_entities(entities, "Remind CUST-0004 and check prd-0002", history="")

    assert grounded.customer_ids == ["CUST-0004"]
    assert grounded.product_ids == ["PRD-0002"]  # found in the text even if the model missed it


def test_ids_from_earlier_messages_are_allowed() -> None:
    entities = empty_entities()
    entities.customer_ids = ["CUST-0004"]

    grounded = ground_entities(entities, "send him a reminder", history="CUST-0004 owes Rs 694")

    assert grounded.customer_ids == ["CUST-0004"]


# --------------------------------------------------------------- supervisor


def test_each_intent_gets_the_specialists_it_needs() -> None:
    assert make_plan(triage("sales_report")) == ["data"]
    assert make_plan(triage("policy_question")) == ["knowledge"]
    assert make_plan(triage("customer_credit")) == ["data", "knowledge", "investigation"]
    assert make_plan(triage("out_of_scope", recommended_route=["data"])) == []
    # Triage can add a specialist, never remove one.
    assert make_plan(triage("sales_report", recommended_route=["knowledge"])) == [
        "data",
        "knowledge",
    ]
    assert make_plan(triage("stock_status", wants_action=True)) == ["data", "investigation"]


def test_the_supervisor_routes_by_what_has_run() -> None:
    plan = ["data", "knowledge", "investigation"]
    base = {"triage": triage("reorder")}

    assert next_step({"triage": None}, plan)[0] == "finalize"
    assert next_step(base, plan)[0] == "data_retrieval"
    assert next_step({**base, "completed_steps": ["data_retrieval"]}, plan)[0] == "knowledge"
    done = ["data_retrieval", "knowledge"]
    assert next_step({**base, "completed_steps": done}, plan)[0] == "investigation"
    looked = [*done, "investigation"]
    assert next_step({**base, "completed_steps": looked}, plan)[0] == "respond"
    more = [*looked, "data_retrieval"]  # the investigation asked for more, and got it
    assert next_step({**base, "completed_steps": more}, plan)[0] == "investigation"
    unclear = {
        "triage": triage("payment_reminder", needs_clarification=True, clarifying_question="Who?")
    }
    assert next_step(unclear, plan)[0] == "clarify"


# ---------------------------------------------------------------- retrieval


def test_the_fallback_plan_follows_the_entities(make_deps) -> None:
    entities = empty_entities().model_dump() | {
        "customer_ids": ["CUST-0001"],
        "product_names": ["toor dal"],
    }
    calls = fallback_calls({"intent": "customer_credit", "entities": entities}, make_deps())

    assert ("get_customer_account", {"customer_id": "CUST-0001"}) in calls
    assert ("search_products", {"query": "toor dal"}) in calls


def test_the_fallback_plan_respects_the_call_budget(make_deps) -> None:
    entities = empty_entities().model_dump() | {
        "product_ids": ["PRD-0001", "PRD-0002", "PRD-0003"],
        "customer_ids": ["CUST-0001", "CUST-0002"],
    }
    calls = fallback_calls(
        {"intent": "stock_discrepancy", "entities": entities}, make_deps(agent_max_tool_calls=4)
    )

    assert len(calls) == 4


# ------------------------------------------------------------ investigation


def investigation(**overrides) -> InvestigationResult:
    values = {
        "issue_type": "over limit",
        "summary": "CUST-0001 is over the limit.",
        "findings": ["CUST-0001 owes Rs 3,900", "CUST-7777 owes Rs 10"],
        "evidence": [
            Evidence(source="policy", reference=CREDIT, fact="Limit is Rs 3,000."),
            Evidence(source="policy", reference="[POL-FAKE-001 v1 §1. X]", fact="Made up."),
            Evidence(
                source="record", reference="get_customer_account CUST-0001", fact="Owes 3,900."
            ),
        ],
        "policy_references": [CREDIT, "POL-FAKE-001 v1 §1. X"],
        "recommended_action": "Block new credit.",
        "proposed_actions": [],
        "confidence": 0.9,
        "requires_human_review": True,
        "needs_more_data": False,
        "data_requests": [],
    }
    values.update(overrides)
    return InvestigationResult(**values)


STATE = {
    "workflow_id": "wf-1",
    "user_query": "How much does CUST-0001 owe?",
    "retrieved_documents": [{"citation": CREDIT, "score": 1.0}],
    "retrieved_data": {
        "get_customer_account(customer_id=CUST-0001)": {"data": {"id": "CUST-0001"}}
    },
    "tool_results": [{"tool": "get_customer_account", "status": "success"}],
}


def test_ungrounded_parts_of_an_investigation_are_dropped() -> None:
    checked, warnings = check_result(investigation(), STATE)

    assert checked["policy_references"] == [CREDIT]
    assert [e["reference"] for e in checked["evidence"]] == [
        CREDIT,
        "get_customer_account CUST-0001",
    ]
    assert checked["findings"] == ["CUST-0001 owes Rs 3,900"]  # CUST-7777 was never fetched
    assert len(warnings) == 3


def test_proposed_actions_must_pass_the_tools_own_validation(make_deps) -> None:
    proposals = [
        ProposedAction(
            tool="send_payment_reminder",
            arguments_json=json.dumps({"customer_id": "CUST-0001"}),
            reason="Overdue.",
        ),
        ProposedAction(
            tool="send_payment_reminder",
            arguments_json=json.dumps({"customer_id": "CUST-0001", "channel": "pigeon"}),
            reason="Bad channel.",
        ),
        ProposedAction(tool="create_case", arguments_json="not json", reason="Broken."),
        ProposedAction(
            tool="send_payment_reminder",
            arguments_json=json.dumps({"customer_id": "CUST-5555"}),
            reason="Unknown customer.",
        ),
    ]

    actions, warnings = check_actions(investigation(proposed_actions=proposals), STATE, make_deps())

    [kept] = actions
    assert kept["arguments"]["customer_id"] == "CUST-0001"
    assert kept["arguments"]["idempotency_key"] == "wf-1:send_payment_reminder:1"
    assert kept["status"] == "proposed"
    assert len(warnings) == 3


# ---------------------------------------------------------------- validator


def draft(answer: str, **extra) -> dict:
    return {
        "answer": answer,
        "facts": [],
        "evidence": [],
        "next_steps": [],
        "pending_approval": [],
        "citations": [],
        "follow_up_question": None,
        **extra,
    }


def test_a_grounded_reply_passes() -> None:
    state = {**STATE, "intent": "customer_credit"}

    assert find_problems(draft(f"CUST-0001 owes Rs 3,900, above the limit {CREDIT}."), state) == []


def test_the_validator_catches_each_kind_of_problem() -> None:
    state = {**STATE, "intent": "customer_credit"}

    unknown_citation = find_problems(draft(f"{CREDIT} and [POL-X-001 v1 §1. Y]"), state)
    no_citation = find_problems(draft("CUST-0001 owes money."), state)
    unknown_id = find_problems(draft(f"CUST-0001 and CUST-0002 owe money {CREDIT}."), state)
    claimed = find_problems(draft(f"I have sent the reminder to CUST-0001 {CREDIT}."), state)
    empty = find_problems(draft(""), {**state, "intent": "sales_report"})

    assert "POL-X-001" in unknown_citation[0]
    assert "cites none" in no_citation[0]
    assert "CUST-0002" in unknown_id[0]
    assert "no action tool confirmed" in claimed[0]
    assert "empty" in empty[0]


@pytest.mark.parametrize(
    "claim",
    [
        "I have successfully sent the reminder to CUST-0001",
        "I\u2019ve also placed the order for CUST-0001",
        "I've gone ahead and sent the reminder to CUST-0001",
        "I have approved the extra credit for CUST-0001",
    ],
)
def test_claims_in_other_words_are_caught(claim: str) -> None:
    state = {**STATE, "intent": "customer_credit"}

    assert any("no action tool" in p for p in find_problems(draft(f"{claim} {CREDIT}."), state))


def test_passive_facts_about_the_records_are_not_claims() -> None:
    state = {
        **STATE,
        "intent": "customer_credit",
        "proposed_actions": [{"tool": "send_payment_reminder"}],
    }
    facts = (
        f"CUST-0001 has been placed on hold before. A reminder has been sent three times "
        f"already {CREDIT}."
    )
    claim = f"The reminder has been sent to CUST-0001 {CREDIT}."

    assert find_problems(draft(facts), state) == []
    assert find_problems(draft(claim), state)


def test_record_facts_in_the_past_tense_are_not_claims() -> None:
    state = {**STATE, "intent": "customer_credit"}
    text = f"A reminder was sent to CUST-0001 on 28 Sep, so wait 7 days {CREDIT}."

    assert find_problems(draft(text), state) == []


# ------------------------------------------------------------------- helpers


def test_reply_rendering_drops_unknown_citations() -> None:
    reply = render_reply(
        draft(
            f"Limit is Rs 3,000 {CREDIT} [POL-X-001 v1 §1. Y].",
            facts=["CUST-0001 owes Rs 3,900"],
            pending_approval=["send_payment_reminder (customer_id=CUST-0001): overdue"],
            completed_actions=[],
        ),
        {CREDIT},
        verified=True,
    )

    assert CREDIT in reply and "POL-X-001" not in reply
    assert "**From your records**\n- CUST-0001 owes Rs 3,900" in reply
    assert "Proposed, not done yet" in reply and "**Done**" not in reply


def test_reducers_merge_instead_of_overwriting() -> None:
    passages = merge_passages(
        [{"citation": "[A v1 §1]", "score": 0.2}], [{"citation": "[A v1 §1]", "score": 0.9}]
    )
    records = merge_records({"a": 1}, {"b": 2})

    assert passages == [{"citation": "[A v1 §1]", "score": 0.9}]
    assert records == {"a": 1, "b": 2}


def test_ids_citations_and_keys() -> None:
    assert ids_in("see prd-0002 and SALE-005668, not PRD-12") == {"PRD-0002", "SALE-005668"}
    assert citations_in(f"x {CREDIT} y [not a citation]") == [CREDIT]
    assert record_key("get_product", {"product_id": "PRD-0002", "x": None}) == (
        "get_product(product_id=PRD-0002)"
    )
