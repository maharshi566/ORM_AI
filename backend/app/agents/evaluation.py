"""Scores the agent graph on a set of cases (the Phase 4 gate; Phase 8 extends it).

For each case the graph runs once, and the result is checked against what the case
expects (see evaluation/datasets/agent_cases.yaml):

* **intent**: triage chose one of the expected intents;
* **records**: the tools returned every expected record ID;
* **documents**: the reply cites at least one expected document;
* **grounded**: the validator passed the reply.

A case is *grounded and cited* when the last three all hold. The gate is that both
the intent accuracy and the grounded-and-cited rate reach the minimum (0.8).
``scripts/eval_agent.py`` runs it with the real model; the tests run it with a
scripted one.
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.agents.common import compact


@dataclass(frozen=True)
class AgentCase:
    id: str
    shop_id: str
    message: str
    intents: tuple[str, ...]
    records: tuple[str, ...] = ()
    documents: tuple[str, ...] = ()


@dataclass
class CaseOutcome:
    case: AgentCase
    intent: str | None
    missing_records: list[str]
    cited_documents: list[str]
    validation: str | None
    latency_ms: float
    input_tokens: int
    output_tokens: int
    answer: str
    errors: list[str] = field(default_factory=list)

    @property
    def intent_ok(self) -> bool:
        return self.intent in self.case.intents

    @property
    def records_ok(self) -> bool:
        return not self.missing_records

    @property
    def documents_ok(self) -> bool:
        if not self.case.documents:
            return True
        return any(doc in self.case.documents for doc in self.cited_documents)

    @property
    def grounded(self) -> bool:
        return self.validation == "PASS"

    @property
    def grounded_and_cited(self) -> bool:
        return self.records_ok and self.documents_ok and self.grounded


@dataclass
class AgentEvalReport:
    outcomes: list[CaseOutcome]
    models: str = ""

    def _share(self, values: list[bool]) -> float:
        return round(sum(values) / len(values), 3) if values else 0.0

    @property
    def intent_accuracy(self) -> float:
        return self._share([o.intent_ok for o in self.outcomes])

    @property
    def grounded_and_cited_rate(self) -> float:
        return self._share([o.grounded_and_cited for o in self.outcomes])

    def passed(self, minimum: float = 0.8) -> bool:
        return self.intent_accuracy >= minimum and self.grounded_and_cited_rate >= minimum


def load_agent_cases(path: Path) -> list[AgentCase]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        AgentCase(
            id=item["id"],
            shop_id=item["shop_id"],
            message=item["message"],
            intents=tuple(item["intent"]),
            records=tuple(item.get("records") or ()),
            documents=tuple(item.get("documents") or ()),
        )
        for item in data["cases"]
    ]


def score(case: AgentCase, state: dict[str, Any], latency_ms: float) -> CaseOutcome:
    fetched = compact(state.get("retrieved_data") or {}, limit=10**8)
    trace = state.get("agent_trace") or []
    cited = [source["citation"].strip("[]").split(" v")[0] for source in state.get("sources") or []]
    return CaseOutcome(
        case=case,
        intent=state.get("intent"),
        missing_records=[record for record in case.records if record not in fetched],
        cited_documents=list(dict.fromkeys(cited)),
        validation=state.get("validation_result"),
        latency_ms=round(latency_ms, 1),
        input_tokens=sum(t.get("input_tokens") or 0 for t in trace),
        output_tokens=sum(t.get("output_tokens") or 0 for t in trace),
        answer=state.get("final_response") or "",
        errors=list(state.get("errors") or []),
    )


async def run_cases(
    cases: list[AgentCase], run_one: Callable[[AgentCase], Awaitable[dict[str, Any]]]
) -> AgentEvalReport:
    outcomes = []
    for case in cases:
        started = time.perf_counter()
        try:
            state = await run_one(case)
        except Exception as exc:  # one broken case must not hide the others
            state = {"errors": [f"{type(exc).__name__}: {exc}"]}
        outcomes.append(score(case, state, (time.perf_counter() - started) * 1000))
    return AgentEvalReport(outcomes=outcomes)


def _mark(ok: bool) -> str:
    return "ok" if ok else "MISS"


def to_markdown(report: AgentEvalReport, *, minimum: float = 0.8) -> str:
    verdict = "PASS" if report.passed(minimum) else "FAIL"
    lines = [
        "# Agent evaluation (Phase 4 gate)",
        "",
        f"Models: {report.models or 'not recorded'}",
        "",
        f"- Intent accuracy: **{report.intent_accuracy:.2f}** (gate {minimum})",
        f"- Grounded and cited: **{report.grounded_and_cited_rate:.2f}** (gate {minimum})",
        f"- Result: **{verdict}**",
        "",
        "| Case | Intent | Records | Documents | Validator | Time (s) | Tokens in/out |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for o in report.outcomes:
        lines.append(
            f"| {o.case.id} | {_mark(o.intent_ok)} {o.intent} | {_mark(o.records_ok)} "
            f"| {_mark(o.documents_ok)} {', '.join(o.cited_documents) or '-'} "
            f"| {o.validation or '-'} | {o.latency_ms / 1000:.1f} "
            f"| {o.input_tokens}/{o.output_tokens} |"
        )
    misses = [o for o in report.outcomes if not (o.intent_ok and o.grounded_and_cited)]
    if misses:
        lines += ["", "## Misses", ""]
        for o in misses:
            lines.append(f"**{o.case.id}** {o.case.message}")
            if not o.intent_ok:
                lines.append(f"- intent {o.intent}, expected {' or '.join(o.case.intents)}")
            if o.missing_records:
                lines.append(f"- records not fetched: {', '.join(o.missing_records)}")
            if not o.documents_ok:
                lines.append(f"- expected a citation of {' or '.join(o.case.documents)}")
            if not o.grounded:
                lines.append(f"- validator: {o.validation}")
            for error in o.errors[:3]:
                lines.append(f"- error: {error}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
