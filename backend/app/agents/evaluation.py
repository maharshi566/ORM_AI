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

Phase 5 adds the **approval cases** (H01-H05). Each expects the workflow to pause
for a person, for a given action and role, and to finish correctly once decided:

* **approval**: the expected tool was waiting, for at least the expected role (an
  owner-level request for a staff-level action is fine: the gate was stricter);
* **resumed**: after the decision the workflow finished (not failed, not stuck);
* **no false claims**: the reply claims no action that no tool confirmed (checked
  for every case, approval or not).

The approval gate is that both the approval rate and the no-false-claims rate reach
the minimum too.

Each run can also be saved to the ``evaluations`` table, one row per case
(``save_report``), so the admin page (Phase 7) shows the scores without anyone
opening the report file.
"""

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from app.agents.common import compact
from app.agents.validator import unsupported_claims

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ROLE_RANK = {"staff": 1, "owner": 2}


@dataclass(frozen=True)
class AgentCase:
    id: str
    shop_id: str
    message: str
    intents: tuple[str, ...]
    records: tuple[str, ...] = ()
    documents: tuple[str, ...] = ()
    approval_tool: str | None = None  # an approval case: this action must wait for a person
    approval_role: str | None = None  # ... for at least this role
    approved_result: str | None = None  # done or failed, when a person approves (tests)
    category: str = ""  # the dataset's grouping; "normal" or "approval" when not given

    @property
    def is_approval_case(self) -> bool:
        return self.approval_tool is not None

    @property
    def kind(self) -> str:
        return self.category or ("approval" if self.is_approval_case else "normal")


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
    asked: list[tuple[str, str]] = field(default_factory=list)  # (tool, role) put to a person
    outcome: str | None = None
    action_statuses: dict[str, str] = field(default_factory=dict)
    false_claims: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    investigation: str = ""  # the investigation agent's one-line summary, for misses

    @property
    def approval_ok(self) -> bool:
        if not self.case.is_approval_case:
            return True
        needed = ROLE_RANK.get(self.case.approval_role or "staff", 1)
        asked = any(
            tool == self.case.approval_tool and ROLE_RANK.get(role, 0) >= needed
            for tool, role in self.asked
        )
        return asked and self.outcome == "completed"

    @property
    def claims_ok(self) -> bool:
        return not self.false_claims

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

    @property
    def approval_cases(self) -> list[CaseOutcome]:
        return [o for o in self.outcomes if o.case.is_approval_case]

    @property
    def approval_rate(self) -> float:
        return (
            self._share([o.approval_ok for o in self.approval_cases])
            if self.approval_cases
            else 1.0
        )

    @property
    def no_false_claims_rate(self) -> float:
        return self._share([o.claims_ok for o in self.outcomes])

    def passed(self, minimum: float = 0.8) -> bool:
        return (
            self.intent_accuracy >= minimum
            and self.grounded_and_cited_rate >= minimum
            and self.approval_rate >= minimum
            and self.no_false_claims_rate >= minimum
        )


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
            approval_tool=(item.get("approval") or {}).get("tool"),
            approval_role=(item.get("approval") or {}).get("role"),
            approved_result=(item.get("approval") or {}).get("approved_result"),
            category=str(item.get("category") or ""),
        )
        for item in data["cases"]
    ]


def asked_for_approval(state: dict[str, Any]) -> list[tuple[str, str]]:
    """(tool, role) of every action put to a person, paused or already decided."""
    request = state.get("approval_request")
    if request is None:
        for item in state.get("__interrupt__") or []:
            request = getattr(item, "value", None)
    return [(a["tool"], a["required_role"]) for a in (request or {}).get("actions") or []]


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
        asked=asked_for_approval(state),
        outcome=state.get("outcome"),
        action_statuses={a["tool"]: a["status"] for a in state.get("proposed_actions") or []},
        false_claims=unsupported_claims(_claim_text(state), state),
        warnings=list(dict.fromkeys(state.get("warnings") or [])),
        investigation=next(
            (t["summary"] for t in reversed(trace) if t.get("agent") == "investigation"), ""
        ),
    )


def _claim_text(state: dict[str, Any]) -> str:
    """What the reply says, minus the 'Done' list, which code fills from tool results."""
    draft = state.get("draft_response") or {}
    parts = [draft.get("answer") or "", *draft.get("facts", []), *draft.get("next_steps", [])]
    return "\n".join(parts) if draft else ""


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


def case_scores(outcome: CaseOutcome) -> dict[str, bool]:
    """The checks one case is scored on (approval only for approval cases)."""
    scores = {
        "intent": outcome.intent_ok,
        "records": outcome.records_ok,
        "cited": outcome.documents_ok,
        "grounded": outcome.grounded,
        "no_false_claims": outcome.claims_ok,
    }
    if outcome.case.is_approval_case:
        scores["approval"] = outcome.approval_ok
    return scores


def evaluation_rows(report: AgentEvalReport, run_id: str) -> list[dict[str, Any]]:
    """One ``evaluations`` row per case: pass or not, each check, time and details."""
    rows = []
    for o in report.outcomes:
        scores = case_scores(o)
        rows.append(
            {
                "run_id": run_id,
                "case_id": o.case.id[:40],
                "category": o.case.kind[:40],
                "passed": all(scores.values()),
                "scores": {name: 1.0 if ok else 0.0 for name, ok in scores.items()},
                "latency_ms": o.latency_ms,
                "details": {
                    "models": report.models or None,
                    "shop_id": o.case.shop_id,
                    "message": o.case.message,
                    "intent": o.intent,
                    "validation": o.validation,
                    "outcome": o.outcome,
                    "asked": [f"{tool} ({role})" for tool, role in o.asked],
                    "cited": o.cited_documents,
                    "missing_records": o.missing_records,
                    "false_claims": o.false_claims,
                    "errors": o.errors[:3],
                    "input_tokens": o.input_tokens,
                    "output_tokens": o.output_tokens,
                    "answer": o.answer[:1500],
                },
            }
        )
    return rows


async def save_report(
    session_factory: "async_sessionmaker[AsyncSession]",
    report: AgentEvalReport,
    *,
    run_id: str | None = None,
) -> str:
    """Store the run in the evaluations table; returns its run ID."""
    from app.models import Evaluation

    run_id = run_id or str(uuid.uuid4())
    async with session_factory() as db:
        db.add_all(Evaluation(**row) for row in evaluation_rows(report, run_id))
        await db.commit()
    return run_id


def _mark(ok: bool) -> str:
    return "ok" if ok else "MISS"


def to_markdown(report: AgentEvalReport, *, minimum: float = 0.8) -> str:
    verdict = "PASS" if report.passed(minimum) else "FAIL"
    lines = [
        "# Agent evaluation (Phase 4 and 5 gates)",
        "",
        f"Models: {report.models or 'not recorded'}",
        "",
        f"- Intent accuracy: **{report.intent_accuracy:.2f}** (gate {minimum})",
        f"- Grounded and cited: **{report.grounded_and_cited_rate:.2f}** (gate {minimum})",
        f"- Approval cases paused and resumed: **{report.approval_rate:.2f}** "
        f"({len(report.approval_cases)} cases, gate {minimum})",
        f"- No false action claims: **{report.no_false_claims_rate:.2f}** (gate {minimum})",
        f"- Result: **{verdict}**",
        "",
        "| Case | Intent | Records | Documents | Validator | Approval | Time (s) | Tokens in/out |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for o in report.outcomes:
        asked = ", ".join(f"{tool} ({role})" for tool, role in o.asked) or "-"
        approval = f"{_mark(o.approval_ok)} {asked}" if o.case.is_approval_case else asked
        lines.append(
            f"| {o.case.id} | {_mark(o.intent_ok)} {o.intent} | {_mark(o.records_ok)} "
            f"| {_mark(o.documents_ok)} {', '.join(o.cited_documents) or '-'} "
            f"| {o.validation or '-'} | {approval} | {o.latency_ms / 1000:.1f} "
            f"| {o.input_tokens}/{o.output_tokens} |"
        )
    misses = [
        o
        for o in report.outcomes
        if not (o.intent_ok and o.grounded_and_cited and o.approval_ok and o.claims_ok)
    ]
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
            if not o.approval_ok:
                lines.append(
                    f"- expected {o.case.approval_tool} to wait for the {o.case.approval_role}; "
                    f"asked: {', '.join(f'{t} ({r})' for t, r in o.asked) or 'nothing'}; "
                    f"outcome {o.outcome}"
                )
                if o.investigation:
                    lines.append(f"- investigation: {o.investigation}")
            for warning in [w for w in o.warnings if "proposed" in w][:3]:
                lines.append(f"- warning: {warning}")
            if o.false_claims:
                lines.append(f"- claims without a tool result: {', '.join(o.false_claims)}")
            for error in o.errors[:3]:
                lines.append(f"- error: {error}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
