"""The Phase 4 gate's 10 normal cases, run with the scripted model.

The real gate runs with a real model (python -m scripts.eval_agent). Here the model is
scripted, so this checks everything around it: that each case's tools return the
records it expects from the seeded data, that the knowledge agent finds the expected
rules with the offline embedder, and that the scoring and report work.
"""

from app.agents.evaluation import load_agent_cases, run_cases, to_markdown
from app.config.paths import BACKEND_DIR

DATASET = BACKEND_DIR / "evaluation" / "datasets" / "agent_cases.yaml"


def test_the_dataset_has_ten_normal_cases() -> None:
    cases = load_agent_cases(DATASET)

    assert len(cases) == 10 and len({c.id for c in cases}) == 10


async def test_the_ten_cases_pass_with_the_scripted_model(make_deps, run_agent) -> None:
    cases = load_agent_cases(DATASET)

    async def run_one(case):
        return await run_agent(make_deps(), case.message, shop_id=case.shop_id)

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
    assert "Result: **PASS**" in markdown and "| A10 |" in markdown
