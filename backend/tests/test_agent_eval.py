"""The 45-case agent evaluation (Phases 4, 5 and 8, then Hinglish), with the scripted model.

The real gate runs with a real model (python -m scripts.eval_agent). Here the model is
scripted, so this checks everything around it: that each case's tools return the
records it expects from the seeded data, that the knowledge agent finds the expected
rules with the offline embedder, that tool failures really fail, that the guardrail
flags the injection cases, and that the scoring and the report work.
"""

import textwrap
import time
from pathlib import Path

import pytest

from app.agents.evaluation import (
    AgentCase,
    AgentEvalReport,
    CaseOutcome,
    ConsistencyReport,
    case_scores,
    consistency_markdown,
    load_agent_cases,
    miss_reasons,
    run_cases,
    to_markdown,
)
from app.agents.judge import grade
from app.config.paths import BACKEND_DIR
from app.tools.api_tools import FaultInjector
from tests.conftest import approval_request
from tests.fake_llm import RuleBasedLLM

DATASET = BACKEND_DIR / "evaluation" / "datasets" / "agent_cases.yaml"


def test_the_dataset_has_45_cases_in_eight_categories() -> None:
    cases = load_agent_cases(DATASET)
    kinds = [c.kind for c in cases]

    assert len(cases) == 45 and len({c.id for c in cases}) == 45
    assert {kind: kinds.count(kind) for kind in set(kinds)} == {
        "normal": 10,
        "approval": 5,
        "ambiguous": 5,
        "missing_information": 5,
        "tool_failure": 5,
        "prompt_injection": 5,
        "policy_conflict": 5,
        "language": 5,
    }
    assert [c.id for c in cases if c.is_approval_case] == ["H01", "H02", "H03", "H04", "H05"]
    # Every Phase 8 case says which route it should take and who should be asked; the
    # language cases are normal questions in Hinglish or Hindi, and say their language.
    phase8 = [c for c in cases if c.kind not in {"normal", "approval", "language"}]
    assert all(c.route and c.escalation for c in phase8)
    assert all(c.language for c in cases if c.kind == "language")


async def test_the_cases_pass_with_the_scripted_model(make_deps, run_agent) -> None:
    cases = load_agent_cases(DATASET)

    async def run_one(case):
        deps = make_deps(faults=FaultInjector(case.fault) if case.fault else None)
        # Cases that propose an action pause for approval; reject, as eval_agent does.
        return await run_agent(deps, case.message, shop_id=case.shop_id, decision="reject")

    report = await run_cases(cases, run_one)

    misses = {o.case.id: miss_reasons(o) for o in report.outcomes if not o.passed}
    assert misses == {}
    assert report.passed()
    assert all(rate == 1.0 for rate in report.rates().values()), report.rates()
    markdown = to_markdown(report)
    assert "**PASS**: 45 of 45 cases" in markdown
    assert "| prompt injection | 5 | 5 |" in markdown and "| P05 |" in markdown


async def test_approved_cases_end_with_the_tools_real_result(make_deps, run_agent) -> None:
    """Approve each approval case as the owner: the expected tool runs, or refuses."""
    for case in [c for c in load_agent_cases(DATASET) if c.is_approval_case]:
        state = await run_agent(
            make_deps(), case.message, shop_id=case.shop_id, decision="approve", role="owner"
        )

        statuses = {a["tool"]: a["status"] for a in state["proposed_actions"]}
        assert statuses[case.approval_tool] == case.approved_result, (case.id, statuses)
        assert state["validation_result"] == "PASS", (case.id, state["validation_feedback"])


async def test_f01_a_follow_up_case_waits_for_staff_and_opens_once_approved(
    make_deps, run_agent
) -> None:
    """F01, as Gemini answered it: the supplier's system is down and the margin is below
    cost, so it proposes a follow-up case. The shopkeeper only asked a question, so the
    case never opens by itself: staff approve it first. Either way F01 passes."""
    [case] = [c for c in load_agent_cases(DATASET) if c.id == "F01"]
    deps = make_deps(RuleBasedLLM(follow_up_case=True), faults=FaultInjector(case.fault))

    paused = await run_agent(deps, case.message, shop_id=case.shop_id)
    [waiting] = approval_request(paused)["actions"]
    assert waiting["tool"] == "create_case" and waiting["required_role"] == "staff"

    approved = await run_agent(
        deps, case.message, shop_id=case.shop_id, decision="approve", user="USR-008", role="staff"
    )
    [opened] = [a for a in approved["proposed_actions"] if a["tool"] == "create_case"]
    assert opened["status"] == "done" and opened["approved_by"] == "USR-008"

    async def run_one(case):
        return await run_agent(deps, case.message, shop_id=case.shop_id, decision="reject")

    [outcome] = (await run_cases([case], run_one)).outcomes
    assert outcome.route_taken == "approval" and outcome.passed, miss_reasons(outcome)


# ------------------------------------------------------------------ scoring rules


def _outcome(case: AgentCase, **extra) -> CaseOutcome:
    values = {
        "intent": case.intents[0],
        "missing_records": [],
        "cited_documents": [],
        "validation": "PASS",
        "latency_ms": 1000.0,
        "input_tokens": 100,
        "output_tokens": 10,
        "answer": "An answer.",
        "outcome": "completed",
    }
    return CaseOutcome(case=case, **{**values, **extra})


def _case(**fields) -> AgentCase:
    return AgentCase(id="X01", shop_id="SHOP-001", message="?", intents=("pricing",), **fields)


def test_a_clarifying_question_is_the_clarify_route_and_needs_no_validator() -> None:
    case = _case(route="clarify", escalation="none")
    asked = _outcome(case, outcome="needs_clarification", validation=None, drafted=False)
    answered = _outcome(case)

    assert case_scores(asked) == {
        "intent": True,
        "records": True,
        "cited": True,
        "grounded": True,
        "no_false_claims": True,
        "route": True,
        "escalation": True,
    }
    assert answered.route_taken == "answer" and not answered.route_ok
    # A written reply the validator never passed is not grounded, even without a draft flag.
    assert not _outcome(case, validation=None, outcome="failed").grounded


def test_escalation_counts_the_strictest_role_asked() -> None:
    owner_case = _case(escalation="owner", route="approval")
    staff_case = _case(escalation="staff")
    none_case = _case(escalation="none")
    by_staff = [("process_return", "staff")]
    by_owner = [("process_return", "staff"), ("update_selling_price", "owner")]

    assert not _outcome(owner_case, asked=by_staff).escalation_ok
    assert _outcome(owner_case, asked=by_owner).escalation_ok
    assert _outcome(owner_case, asked=by_owner).route_taken == "approval"
    assert _outcome(staff_case, asked=by_owner).escalation_ok  # stricter is fine
    assert not _outcome(staff_case).escalation_ok  # but nobody asked is not
    assert not _outcome(none_case, asked=by_staff).escalation_ok


def test_a_tool_failure_case_needs_the_tool_to_have_really_failed() -> None:
    case = _case(
        tools=("get_product", "check_supplier_price"), tool_errors=("check_supplier_price",)
    )

    failed = _outcome(
        case,
        tools_called=["get_product", "check_supplier_price"],
        tool_failures={"check_supplier_price": "timeout"},
    )
    worked = _outcome(case, tools_called=["get_product", "check_supplier_price"])
    skipped = _outcome(case, tools_called=["get_product"])

    assert failed.tools_ok
    assert not worked.tools_ok and miss_reasons(worked) == [
        "expected to fail but did not: check_supplier_price"
    ]
    assert not skipped.tools_ok and "tools not called: check_supplier_price" in miss_reasons(
        skipped
    )


def test_the_guardrail_check_works_both_ways() -> None:
    must_flag, must_not = _case(flagged=True), _case(flagged=False)

    assert _outcome(must_flag, input_flags=["skip_approval"]).guardrail_ok
    assert not _outcome(must_flag).guardrail_ok
    assert _outcome(must_not).guardrail_ok
    assert not _outcome(must_not, input_flags=["override_instructions"]).guardrail_ok
    assert "guardrail" not in case_scores(_outcome(_case()))  # only scored when stated


def test_an_allowed_action_put_to_a_person_is_also_right() -> None:
    case = _case(route="answer", escalation="none", may_propose=("create_case",))
    answered = _outcome(case)
    followed_up = _outcome(case, asked=[("create_case", "staff")])
    repriced = _outcome(case, asked=[("update_selling_price", "owner")])
    both = _outcome(case, asked=[("create_case", "staff"), ("update_selling_price", "owner")])

    assert answered.passed and followed_up.passed
    assert not repriced.route_ok and not repriced.escalation_ok
    assert not both.passed  # one allowed action does not excuse another
    assert miss_reasons(repriced)[0] == (
        "route approval, expected answer (or approval of create_case)"
    )


def test_the_report_breaks_results_down_by_category() -> None:
    clarify = AgentCase(
        id="Q01",
        shop_id="SHOP-001",
        message="Is it in stock?",
        intents=("stock_status",),
        category="ambiguous",
        route="clarify",
    )
    normal = AgentCase(id="A01", shop_id="SHOP-001", message="Low?", intents=("reorder",))
    report = AgentEvalReport(
        outcomes=[
            _outcome(normal, latency_ms=2000.0),
            _outcome(clarify, latency_ms=4000.0),  # answered instead of asking: a miss
        ]
    )

    assert report.by_category() == [("normal", 1, 1), ("ambiguous", 1, 0)]
    assert report.route_accuracy == 0.0 and not report.passed()
    assert report.latency_p50_ms == 2000.0 and report.latency_p95_ms == 4000.0
    markdown = to_markdown(report)
    assert "**FAIL**: 1 of 2 cases" in markdown
    assert "- route answer, expected clarify" in markdown


async def test_the_judge_grades_each_reply_and_the_report_shows_it(make_deps, run_agent) -> None:
    cases = [c for c in load_agent_cases(DATASET) if c.id in {"A07", "Q01"}]
    llm = RuleBasedLLM()

    async def run_one(case):
        return await run_agent(make_deps(llm), case.message, shop_id=case.shop_id)

    async def judge(case, state, outcome):
        return await grade(llm, case, state, outcome.route_taken)

    report = await run_cases(cases, run_one, judge=judge)

    assert all(o.quality and 1 <= o.quality["overall"] <= 5 for o in report.outcomes)
    assert set(report.quality_means()) == {
        "correctness",
        "helpfulness",
        "clarity",
        "language",
        "overall",
    }
    markdown = to_markdown(report)
    assert "## Answer quality (AI judge, 1 to 5)" in markdown and "Lowest graded:" in markdown
    assert "Not part of the gate" in markdown
    assert report.passed()  # the grades never change the gate


async def test_a_failing_judge_leaves_the_case_scored_but_ungraded() -> None:
    async def run_one(case):
        return {}

    async def broken(case, state, outcome):
        raise RuntimeError("judge down")

    [outcome] = (await run_cases([_case()], run_one, judge=broken)).outcomes

    assert outcome.quality is None and "the judge failed (RuntimeError)" in outcome.warnings


async def test_the_run_stops_when_the_quota_runs_out() -> None:
    cases = [
        AgentCase(id=f"X0{n}", shop_id="SHOP-001", message="?", intents=("pricing",))
        for n in range(1, 7)
    ]
    ran = []

    async def run_one(case):
        ran.append(case.id)
        return {"errors": ["triage failed: Google Gemini: rate limit reached (free quota)"]}

    report = await run_cases(cases, run_one, stop_on_quota=True)

    assert ran == ["X01", "X02", "X03"] and len(report.outcomes) == 3
    assert "3 cases in a row" in report.stopped
    assert "**Incomplete:**" in to_markdown(report)


def test_repeated_runs_show_flaky_cases_and_the_spread_of_each_rate() -> None:
    steady, shaky = (
        _case(),
        AgentCase(id="X02", shop_id="SHOP-001", message="?", intents=("pricing",)),
    )
    first = AgentEvalReport(outcomes=[_outcome(steady), _outcome(shaky)])
    second = AgentEvalReport(outcomes=[_outcome(steady), _outcome(shaky, intent="returns")])

    consistency = ConsistencyReport([first, second])

    assert consistency.flaky() == [("X02", 1, 2)]
    assert consistency.always_pass_rate == 0.5 and consistency.ever_pass_rate == 1.0
    assert consistency.rate_spread()["intent accuracy"] == (0.5, 0.75, 1.0)
    markdown = consistency_markdown(consistency)
    assert "pass^2" in markdown and "**X02** passed 1 of 2" in markdown
    assert "intent returns, expected pricing" in markdown


async def test_the_pause_between_cases_is_not_counted_as_case_time() -> None:
    """The first Gemini run counted --pause 30 inside every case after the first, so
    a two-second clarifying question showed as 32 seconds."""
    cases = [
        AgentCase(id=f"X0{n}", shop_id="SHOP-001", message="?", intents=("pricing",))
        for n in (1, 2, 3)
    ]
    started = time.perf_counter()

    async def run_one(case):
        return {}

    report = await run_cases(cases, run_one, pause_s=0.2)

    assert time.perf_counter() - started >= 0.4  # waited between cases, not before the first
    assert all(o.latency_ms < 100 for o in report.outcomes)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("route", "reply", "route must be one of"),
        ("escalation", "manager", "escalation must be one of"),
        ("fault", "explode", "fault must be one of"),
        ("category", "weird", "category must be one of"),
        ("may_propose", "[delete_everything]", "may_propose lists unknown action tools"),
    ],
)
def test_a_typo_in_the_dataset_is_refused(tmp_path: Path, field, value, message) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text(
        textwrap.dedent(f"""
            cases:
              - id: X01
                shop_id: SHOP-001
                message: Hello
                intent: [general_help]
                {field}: {value}
        """),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_agent_cases(path)


# ------------------------------------------------------------------ the script


def test_model_names_that_move_are_not_pinned() -> None:
    from app.agents.evaluation import unpinned_models

    assert unpinned_models(["gemini-3.8-flash", "gemini-flash-latest", "llama3:latest", ""]) == [
        "gemini-flash-latest",
        "llama3:latest",
    ]


def test_the_script_refuses_a_model_that_can_change_under_it(monkeypatch, capsys) -> None:
    from app.config.settings import Settings
    from scripts import eval_agent

    settings = Settings(
        _env_file=None,
        llm_base_url="https://gateway.example/v1",
        llm_api_key="test-key",
        llm_model_fast="gemini-flash-latest",
        llm_model_smart="gemini-3.8-flash",
    )
    monkeypatch.setattr(eval_agent, "get_settings", lambda: settings)

    assert eval_agent.main([]) == 2
    assert "gemini-flash-latest can change without notice" in capsys.readouterr().err


def test_the_script_runs_one_case_or_one_category() -> None:
    from scripts import eval_agent

    cases = load_agent_cases(DATASET)
    one = eval_agent.select_cases(cases, eval_agent.parse_args(["--case", "h02"]))
    failures = eval_agent.select_cases(
        cases, eval_agent.parse_args(["--category", "tool_failure", "--temperature", "0"])
    )

    assert [c.id for c in one] == ["H02"]
    assert [c.id for c in failures] == ["F01", "F02", "F03", "F04", "F05"]
