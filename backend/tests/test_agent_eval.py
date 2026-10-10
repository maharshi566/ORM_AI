"""The Phase 4 and 5 gates' cases (10 normal, 5 approval), run with the scripted model.

The real gate runs with a real model (python -m scripts.eval_agent). Here the model is
scripted, so this checks everything around it: that each case's tools return the
records it expects from the seeded data, that the knowledge agent finds the expected
rules with the offline embedder, and that the scoring and report work.
"""

from app.agents.evaluation import load_agent_cases, run_cases, to_markdown
from app.config.paths import BACKEND_DIR

DATASET = BACKEND_DIR / "evaluation" / "datasets" / "agent_cases.yaml"


def test_the_dataset_has_ten_normal_and_five_approval_cases() -> None:
    cases = load_agent_cases(DATASET)

    assert len(cases) == 15 and len({c.id for c in cases}) == 15
    assert [c.id for c in cases if c.is_approval_case] == ["H01", "H02", "H03", "H04", "H05"]


async def test_the_cases_pass_with_the_scripted_model(make_deps, run_agent) -> None:
    cases = load_agent_cases(DATASET)

    async def run_one(case):
        # Cases that propose an action pause for approval; reject, as eval_agent does.
        return await run_agent(make_deps(), case.message, shop_id=case.shop_id, decision="reject")

    report = await run_cases(cases, run_one)

    misses = {
        o.case.id: (o.missing_records, o.cited_documents, o.validation)
        for o in report.outcomes
        if not o.grounded_and_cited
    }
    assert report.intent_accuracy == 1.0
    assert misses == {}
    assert report.passed()
    markdown = to_markdown(report)
    assert "Result: **PASS**" in markdown and "| H05 |" in markdown
    assert report.approval_rate == 1.0 and report.no_false_claims_rate == 1.0


async def test_approved_cases_end_with_the_tools_real_result(make_deps, run_agent) -> None:
    """Approve each approval case as the owner: the expected tool runs, or refuses."""
    for case in [c for c in load_agent_cases(DATASET) if c.is_approval_case]:
        state = await run_agent(
            make_deps(), case.message, shop_id=case.shop_id, decision="approve", role="owner"
        )

        statuses = {a["tool"]: a["status"] for a in state["proposed_actions"]}
        assert statuses[case.approval_tool] == case.approved_result, (case.id, statuses)
        assert state["validation_result"] == "PASS", (case.id, state["validation_feedback"])
