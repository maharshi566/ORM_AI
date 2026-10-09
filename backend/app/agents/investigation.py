"""Investigation agent: works out what is going on and what should be done.

Combines the records (from the data agent) with the shop's rules (from the knowledge
agent) into structured findings: evidence, the rules applied, a recommended action,
actions ORM_AI could take, and a confidence score. Uses the smart model with
structured output (``InvestigationResult``). Holds no tools.

Its output is checked in code before anyone relies on it:

* a policy reference is kept only if it is the citation of a passage actually
  retrieved, and record evidence only if it names a tool that ran or a known ID;
* a finding that mentions an ID no tool returned is dropped;
* a proposed action must be an action tool whose arguments pass that tool's own
  input validation, and gets an idempotency key tied to this workflow, so a resumed
  workflow can never perform it twice. Proposed actions are never run here; Phase 5
  adds the approval step that runs them.

It decides where the workflow goes next (the "decision node"): back to data retrieval
when a specific record is missing (at most AGENT_MAX_LOOPS times), to human review
when there are actions or doubts, or straight to the response.
"""

import json
from typing import Any

from pydantic import ValidationError

from app.agents.common import (
    AgentOutcome,
    allowed_citations,
    citations_in,
    ids_in,
    known_ids,
    render_passages,
    render_records,
    request_header,
)
from app.agents.schemas import InvestigationResult
from app.graph.deps import AgentDeps
from app.prompts.investigation_prompt import INVESTIGATION_PROMPT

AGENT = "investigation"


def _normalise_citation(text: str) -> str:
    text = text.strip()
    return text if text.startswith("[") else f"[{text.strip('[]')}]"


def check_result(
    result: InvestigationResult, state: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """The result with ungrounded parts removed, plus a warning for each removal."""
    warnings: list[str] = []
    allowed = allowed_citations(state)
    known = known_ids(state)
    called_ok = {
        entry["tool"] for entry in state.get("tool_results") or [] if entry["status"] == "success"
    }

    references = []
    for raw in result.policy_references:
        citation = _normalise_citation(raw)
        if citation in allowed:
            references.append(citation)
        else:
            warnings.append(f"Dropped a policy reference that was not retrieved: {raw[:80]}")

    evidence = []
    for item in result.evidence:
        if item.source == "policy":
            grounded = any(c in allowed for c in citations_in(item.reference)) or (
                _normalise_citation(item.reference) in allowed
            )
        elif item.source == "record":
            mentioned = ids_in(f"{item.reference} {item.fact}")
            grounded = (any(tool in item.reference for tool in called_ok) or bool(mentioned)) and (
                mentioned <= known
            )
        else:
            grounded = True  # what the user said
        if grounded:
            evidence.append(item.model_dump())
        else:
            warnings.append(f"Dropped evidence without a matching source: {item.reference[:80]}")

    findings = []
    for finding in result.findings:
        unknown = ids_in(finding) - known
        if unknown:
            warnings.append(
                f"Dropped a finding about unknown records: {', '.join(sorted(unknown))}"
            )
        else:
            findings.append(finding)

    data = result.model_dump()
    data.update(policy_references=references, evidence=evidence, findings=findings)
    return data, warnings


def check_actions(
    result: InvestigationResult, state: dict[str, Any], deps: AgentDeps
) -> tuple[list[dict[str, Any]], list[str]]:
    """Proposed actions whose arguments pass the tool's own validation."""
    actions: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, proposal in enumerate(result.proposed_actions, start=1):
        spec = deps.registry.spec(proposal.tool)
        try:
            arguments = json.loads(proposal.arguments_json or "{}")
        except json.JSONDecodeError:
            arguments = None
        if spec is None or spec.kind != "action" or not isinstance(arguments, dict):
            warnings.append(f"Dropped a proposed {proposal.tool}: its arguments were not usable.")
            continue
        arguments.pop("idempotency_key", None)
        if "idempotency_key" in spec.input_model.model_fields:
            arguments["idempotency_key"] = f"{state['workflow_id']}:{proposal.tool}:{index}"
        try:
            checked = spec.input_model.model_validate(arguments).model_dump(mode="json")
        except ValidationError as exc:
            problems = "; ".join(e["msg"] for e in exc.errors(include_url=False)[:3])
            warnings.append(f"Dropped a proposed {proposal.tool}: {problems}")
            continue
        unknown = ids_in(json.dumps(checked)) - known_ids(state)
        if unknown:
            warnings.append(
                f"Dropped a proposed {proposal.tool} about unknown records: "
                + ", ".join(sorted(unknown))
            )
            continue
        actions.append(
            {
                "tool": proposal.tool,
                "arguments": checked,
                "reason": proposal.reason,
                "status": "proposed",
            }
        )
    return actions, warnings


async def run(state: dict[str, Any], deps: AgentDeps) -> AgentOutcome:
    loops = state.get("retrieval_loops") or 0
    max_loops = deps.settings.agent_max_loops
    task = "\n\n".join(
        [
            request_header(state, deps.now),
            render_records(state),
            render_passages(state),
            f"This is investigation round {loops + 1}. More data can be requested "
            f"{max(0, max_loops - loops)} more time(s).",
            "Investigate and return the result.",
        ]
    )
    reply = await deps.llm.structured(
        InvestigationResult,
        system=INVESTIGATION_PROMPT.render(),
        messages=[{"role": "user", "content": task}],
        tier="smart",
    )
    result = reply.value
    checked, warnings = check_result(result, state)
    actions, action_warnings = check_actions(result, state, deps)
    warnings += action_warnings

    update: dict[str, Any] = {
        "investigation_result": checked,
        "confidence": result.confidence,
        "proposed_actions": actions,
        "completed_steps": [AGENT],
        "warnings": warnings,
    }
    if result.needs_more_data and result.data_requests and loops < max_loops:
        update.update(route="data_retrieval", data_requests=result.data_requests[:5])
        update["retrieval_loops"] = loops + 1
        decision = f"needs more data ({'; '.join(result.data_requests[:2])})"
    elif actions or result.requires_human_review:
        update["route"] = "human_review"
        decision = f"{len(actions)} action(s) proposed" + (
            ", human review" if result.requires_human_review else ""
        )
    else:
        update["route"] = "respond"
        decision = "ready to answer"
    return AgentOutcome(
        update=update,
        summary=f"{result.issue_type}: {decision}; confidence {result.confidence:.2f}",
        usage=reply.usage,
    )
