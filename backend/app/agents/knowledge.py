"""Knowledge agent: finds the shop rules and procedures that apply, with citations.

It runs the ``search_knowledge`` tool (Phase 3's hybrid search over ChromaDB) through
the ToolRegistry, with up to two searches: the request itself, and a standard policy
question for the intent (for example "customer credit limit and overdue dues" for a
credit question). The second search makes sure the governing rule is found even when
the shopkeeper's words do not mention it. Passages from both are merged by citation.

No model call is needed to choose these searches, so this agent is fast and free; the
model reads the passages later, in the investigation and response agents. Supplier
flyers (untrusted outside material) are searched only when the request is about an
offer or scheme, and they stay marked as untrusted.
"""

import re
from typing import Any

from app.agents.common import AgentOutcome
from app.graph.deps import AgentDeps

AGENT = "knowledge"
TOP_PASSAGES = 6

INTENT_QUERIES: dict[str, str] = {
    "stock_status": "reorder level and keeping stock",
    "reorder": "when to reorder and how to place a purchase order",
    "customer_credit": "customer credit limit and overdue dues rules",
    "payment_reminder": "when payment reminders may be sent",
    "supplier_issue": "late or short supplier deliveries: what to do",
    "stock_discrepancy": "stock count mismatch and stock adjustment approval",
    "pricing": "selling price, margin and price change rules",
    "returns": "customer returns and refunds",
    "general_help": "what ORM_AI can do and how to use it",
}
OFFER_WORDS = re.compile(r"\b(flyer|offer|scheme|promotion|festival|discount)s?\b", re.IGNORECASE)


def queries_for(state: dict[str, Any]) -> list[str]:
    triage = state.get("triage") or {}
    first = (triage.get("summary") or state.get("user_query") or "").strip()
    queries = [first[:500]] if len(first) >= 3 else []
    extra = INTENT_QUERIES.get(state.get("intent") or "")
    if extra and extra not in queries:
        queries.append(extra)
    return queries


async def run(state: dict[str, Any], deps: AgentDeps) -> AgentOutcome:
    include_untrusted = bool(OFFER_WORDS.search(state.get("user_query") or ""))
    passages: list[dict[str, Any]] = []
    log: list[dict[str, Any]] = []
    errors: list[str] = []
    async with deps.session_factory() as session:
        for query in queries_for(state):
            arguments = {"query": query, "top_k": 5, "include_untrusted": include_untrusted}
            result = await deps.call_tool(session, state, AGENT, "search_knowledge", arguments)
            log.append(
                {
                    "agent": AGENT,
                    "tool": "search_knowledge",
                    "arguments": arguments,
                    "status": result.status,
                    "error_code": str(result.error_code) if result.error_code else None,
                    "latency_ms": result.latency_ms,
                }
            )
            if result.ok and result.data:
                passages.extend(result.data["passages"])
            else:
                errors.append(f"search_knowledge failed ({result.error_code})")
    best: dict[str, dict[str, Any]] = {}
    for passage in passages:
        kept = best.get(passage["citation"])
        if kept is None or passage["score"] > kept["score"]:
            best[passage["citation"]] = passage
    top = sorted(best.values(), key=lambda p: p["score"], reverse=True)[:TOP_PASSAGES]
    cited = ", ".join(p["citation"] for p in top[:3]) or "nothing found"
    return AgentOutcome(
        update={
            "retrieved_documents": top,
            "tool_results": log,
            "completed_steps": [AGENT],
            "errors": errors,
        },
        summary=f"{len(log)} searches, {len(top)} passages: {cited}",
    )
