"""The graph's nodes: each agent wrapped with timing, logging and error handling.

``agent_node`` turns an agent's ``run(state, deps)`` into a LangGraph node. Around
every agent it:

* times the run and records model, tokens and a one-line summary in the state's
  ``agent_trace`` and in the ``agent_runs`` table;
* emits "agent_started" / "agent_finished" events (the live progress stream, Phase 6);
* catches failures. A model error (``LLMError``) becomes a clear message in
  ``errors`` and the agent's name in ``failed_agents``; the workflow carries on with
  what it has, and the finalize node explains what is missing. A bug becomes a short
  "internal error" and is logged with its traceback.

The nodes that are not agents (clarify, finalize) are defined here too. Human review
(the policy gate and the approval pause) lives in app/agents/human_review.py and the
action agent in app/agents/action.py.

A pause for approval is not a failure: LangGraph signals it by raising
``GraphInterrupt``, which the wrapper lets through untouched.
"""

import time
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.runtime import Runtime

from app.agents import (
    action,
    human_review,
    investigation,
    knowledge,
    response,
    retrieval,
    supervisor,
    triage,
    validator,
)
from app.agents.common import AgentOutcome, citations_in
from app.agents.response import describe_action, describe_result
from app.core.logging import get_logger
from app.graph.deps import AgentDeps
from app.models.types import utcnow
from app.services.chat_model import LLMError

logger = get_logger(__name__)

AgentRun = Callable[[dict[str, Any], AgentDeps], Awaitable[AgentOutcome]]
Node = Callable[[dict[str, Any], Runtime[AgentDeps]], Awaitable[dict[str, Any]]]

# Where the graph goes if an agent fails, so a failure never leaves the route stale.
ON_FAILURE: dict[str, str] = {
    "investigation": "respond",
    "human_review": "respond",  # nothing runs; the reply says what was proposed
    "action": "respond",
    "validate": "finalize",
    "supervisor": "finalize",
}

BLOCKED_REPLY = (
    "I stopped this answer because it would have followed instructions from an outside "
    "document, such as a supplier flyer. The shop's rules do not allow that (POL-AI-001 "
    "§3), and nothing was changed. Please check that document with the owner, or ask me "
    "again in different words."
)


def blocked_reply(state: dict[str, Any]) -> str:
    """The fixed BLOCK reply, honest about any approved action that already ran."""
    done = [a for a in state.get("proposed_actions") or [] if a.get("status") == "done"]
    if not done:
        return BLOCKED_REPLY
    lines = [
        "I stopped this answer because it would have followed instructions from an "
        "outside document, such as a supplier flyer, which the shop's rules do not allow "
        "(POL-AI-001 §3).",
        "\n**Done before that, as approved** (only these were changed):",
        *[f"- {describe_result(a)}" for a in done],
        "\nPlease check that document with the owner, or ask me again in different words.",
    ]
    return "\n".join(lines)


def agent_node(name: str, run: AgentRun) -> Node:
    async def node(state: dict[str, Any], runtime: Runtime[AgentDeps]) -> dict[str, Any]:
        deps = runtime.context
        started_at = utcnow()
        started = time.perf_counter()
        await deps.emit({"type": "agent_started", "agent": name})
        error: str | None = None
        try:
            outcome = await run(state, deps)
        except GraphBubbleUp:
            raise  # a pause for approval (interrupt), handled by LangGraph
        except LLMError as err:
            error = err.message
            outcome = AgentOutcome(
                update={
                    "errors": [f"{name}: {err.message}"],
                    "failed_agents": [name],
                    "completed_steps": [name],  # counted as done, so it is not retried forever
                },
                summary=f"failed ({err.kind})",
            )
        except Exception as exc:
            logger.exception("agent_crashed", agent=name, error_type=type(exc).__name__)
            error = f"internal error ({type(exc).__name__})"
            outcome = AgentOutcome(
                update={
                    "errors": [f"{name}: {error}"],
                    "failed_agents": [name],
                    "completed_steps": [name],
                },
                summary="failed (internal error)",
            )
        if error and name in ON_FAILURE:
            outcome.update["route"] = ON_FAILURE[name]
        usage = outcome.usage
        trace = {
            "agent": name,
            "status": "error" if error else "success",
            "model": usage.model or None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "llm_calls": usage.calls,
            "fallback_model_used": usage.fallback_used,
            "summary": outcome.summary,
            "error": error,
        }
        logger.info("agent_run", workflow_id=state.get("workflow_id"), **trace)
        await deps.record_run(state.get("workflow_id"), trace, started_at)
        await deps.emit({"type": "agent_finished", **trace})
        return {**outcome.update, "agent_trace": [trace]}

    node.__name__ = name
    return node


# ---------------------------------------------------------------- non-agent nodes


async def _clarify(state: dict[str, Any], deps: AgentDeps) -> AgentOutcome:
    question = (state.get("triage") or {}).get("clarifying_question") or (
        "Could you tell me a little more about what you need?"
    )
    return AgentOutcome(
        update={"final_response": question, "outcome": "needs_clarification"},
        summary="asked a clarifying question",
    )


def render_reply(draft: dict[str, Any], allowed: set[str], *, verified: bool) -> str:
    """The FinalResponse as Markdown, with any unknown citation removed."""

    def clean(text: str) -> str:
        for citation in citations_in(text):
            if citation not in allowed:
                text = text.replace(citation, "")
        return " ".join(text.split())

    lines = [clean(draft.get("answer") or "")]
    sections = [
        ("From your records", draft.get("facts")),
        ("Shop rules that apply", draft.get("evidence")),
        ("Suggested next steps", draft.get("next_steps")),
        ("Proposed, not done yet (needs your approval)", draft.get("pending_approval")),
        ("Done", draft.get("completed_actions")),
    ]
    for title, items in sections:
        items = [clean(item) for item in items or [] if clean(item)]
        if items:
            lines.append(f"\n**{title}**\n" + "\n".join(f"- {item}" for item in items))
    if draft.get("follow_up_question"):
        lines.append(f"\n{clean(draft['follow_up_question'])}")
    if not verified:
        lines.append(
            "\n_Note: parts of this answer could not be fully checked against your records._"
        )
    return "\n".join(lines).strip()


def _fallback_reply(state: dict[str, Any]) -> str:
    """When no model-written reply exists: say what failed and what was found."""
    errors = state.get("errors") or ["the assistant could not finish this request"]
    lines = ["Sorry, I could not complete this request.", f"Reason: {errors[0]}"]
    found = [
        key
        for key, record in (state.get("retrieved_data") or {}).items()
        if record.get("status") == "success"
    ]
    if found:
        lines.append("Records I did fetch: " + ", ".join(found[:6]))
    actions = state.get("proposed_actions") or []
    if actions:
        lines.append("Proposed (not done): " + "; ".join(describe_action(a) for a in actions))
    return "\n".join(lines)


async def _finalize(state: dict[str, Any], deps: AgentDeps) -> AgentOutcome:
    allowed = {p["citation"]: p for p in state.get("retrieved_documents") or []}
    draft = state.get("draft_response")
    if state.get("outcome") == "needs_clarification":
        return AgentOutcome(update={}, summary="finished: asked for clarification")
    if state.get("final_response") and not draft:  # e.g. the out-of-scope reply
        return AgentOutcome(update={"outcome": "completed"}, summary="finished: fixed reply")
    if state.get("validation_result") == "BLOCK":
        return AgentOutcome(
            update={"final_response": blocked_reply(state), "outcome": "blocked", "sources": []},
            summary="finished: blocked",
        )
    if not draft:
        return AgentOutcome(
            update={"final_response": _fallback_reply(state), "outcome": "failed", "sources": []},
            summary="finished: failed",
        )
    verified = state.get("validation_result") == "PASS"
    reply = render_reply(draft, set(allowed), verified=verified)
    cited = [
        c for c in dict.fromkeys(citations_in(reply) + draft.get("citations", [])) if c in allowed
    ]
    sources = [
        {
            "citation": c,
            "title": allowed[c]["title"],
            "section": allowed[c]["section"],
            "excerpt": allowed[c]["text"][:300],
            "trust": allowed[c]["trust"],
        }
        for c in cited
    ]
    return AgentOutcome(
        update={"final_response": reply, "sources": sources, "outcome": "completed"},
        summary=f"finished: {len(sources)} source(s)"
        + ("" if verified else ", not fully verified"),
    )


triage_node = agent_node("triage", triage.run)
supervisor_node = agent_node("supervisor", supervisor.run)
data_retrieval_node = agent_node("data_retrieval", retrieval.run)
knowledge_node = agent_node("knowledge", knowledge.run)
investigation_node = agent_node("investigation", investigation.run)
human_review_node = agent_node("human_review", human_review.run)
action_node = agent_node("action", action.run)
respond_node = agent_node("respond", response.run)
validate_node = agent_node("validate", validator.run)
clarify_node = agent_node("clarify", _clarify)
finalize_node = agent_node("finalize", _finalize)
