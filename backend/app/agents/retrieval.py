"""Data retrieval agent: fetches the shop records a request needs.

The fast model chooses read tools (``registry.schemas_for("data_retrieval")``), the
agent runs them through the ToolRegistry (shop-scoped, validated, logged) and sends
the results back, for up to ``MAX_ROUNDS`` rounds and ``AGENT_MAX_TOOL_CALLS`` calls.
The model only ever *asks* for data: every value the other agents see came from a
tool, never from the model.

Fallback: if the model cannot call tools (an error, or a model or gateway without tool
support), a fixed plan built from the triage entities runs instead, for example
``get_customer_account`` for each customer ID. The answer is then still grounded,
just less tailored, and a warning says why.
"""

import json
from datetime import timedelta
from typing import Any

from app.agents.common import AgentOutcome, compact, record_key, request_header
from app.graph.deps import AgentDeps
from app.prompts.retrieval_prompt import RETRIEVAL_PROMPT
from app.services.chat_model import LLMError, LLMUsage
from app.tools.base import ToolResult

AGENT = "data_retrieval"
MAX_ROUNDS = 4


def fallback_calls(state: dict[str, Any], deps: AgentDeps) -> list[tuple[str, dict[str, Any]]]:
    """A fixed plan from the triage entities, used when the model cannot call tools."""
    entities = state.get("entities") or {}
    intent = state.get("intent") or ""
    calls: list[tuple[str, dict[str, Any]]] = []
    for product_id in entities.get("product_ids", [])[:3]:
        calls.append(("get_product", {"product_id": product_id}))
        if intent == "stock_discrepancy":
            calls.append(("get_stock_movements", {"product_id": product_id, "days": 30}))
        if intent == "pricing":
            calls.append(("check_supplier_price", {"product_id": product_id}))
    for customer_id in entities.get("customer_ids", [])[:3]:
        calls.append(("get_customer_account", {"customer_id": customer_id}))
    for supplier_id in entities.get("supplier_ids", [])[:2]:
        calls.append(("get_supplier", {"supplier_id": supplier_id}))
    for sale_id in entities.get("sale_ids", [])[:2]:
        calls.append(("get_sale", {"sale_id": sale_id}))
    if not entities.get("product_ids"):
        for name in entities.get("product_names", [])[:2]:
            if len(name.strip()) >= 2:
                calls.append(("search_products", {"query": name.strip()[:60]}))
    if not entities.get("customer_ids"):
        for name in entities.get("customer_names", [])[:2]:
            if len(name.strip()) >= 2:
                calls.append(("search_customers", {"query": name.strip()[:60]}))
    if entities.get("purchase_order_ids") or intent == "supplier_issue":
        calls.append(
            ("get_purchase_orders", {"statuses": ["placed", "partially_received"], "limit": 50})
        )
    if intent in {"reorder", "stock_status"} and not entities.get("product_ids"):
        calls.append(("get_low_stock_products", {}))
    if intent == "sales_report":
        end = deps.now.date()
        start = end - timedelta(days=6)
        calls.append(
            (
                "get_sales_summary",
                {
                    "start_date": entities.get("date_from") or start.isoformat(),
                    "end_date": entities.get("date_to") or end.isoformat(),
                },
            )
        )
    unique = {record_key(name, args): (name, args) for name, args in calls}
    return list(unique.values())[: deps.settings.agent_max_tool_calls]


def _task(state: dict[str, Any], deps: AgentDeps) -> str:
    parts = [request_header(state, deps.now)]
    if state.get("data_requests"):
        parts.append(
            "The investigation agent needs these records as well:\n- "
            + "\n- ".join(state["data_requests"])
        )
    if state.get("retrieved_data"):
        parts.append(
            "Already fetched (do not fetch again):\n- " + "\n- ".join(state["retrieved_data"])
        )
    parts.append("Fetch the records needed to answer the request.")
    return "\n\n".join(parts)


def _record(result: ToolResult) -> dict[str, Any]:
    if result.ok:
        return {"status": "success", "data": result.data}
    return {
        "status": "error",
        "error_code": str(result.error_code),
        "error_message": result.error_message,
    }


def _for_model(result: ToolResult) -> str:
    if result.ok:
        return compact(result.data)
    return compact({"error": str(result.error_code), "message": result.error_message})


class _Visit:
    """The tool calls of one visit: results, the log, and the call budget."""

    def __init__(self, state: dict[str, Any], deps: AgentDeps, session: Any) -> None:
        self.state, self.deps, self.session = state, deps, session
        self.records: dict[str, Any] = {}
        self.log: list[dict[str, Any]] = []
        self.budget = deps.settings.agent_max_tool_calls

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        key = record_key(name, arguments)
        earlier = (self.state.get("retrieved_data") or {}).get(key) or self.records.get(key)
        if earlier is not None and earlier.get("status") == "success":
            return ToolResult(tool=name, status="success", data=earlier["data"])
        self.budget -= 1
        result = await self.deps.call_tool(self.session, self.state, AGENT, name, arguments)
        self.records[key] = {"tool": name, "arguments": arguments, **_record(result)}
        self.log.append(
            {
                "agent": AGENT,
                "tool": name,
                "arguments": arguments,
                "status": result.status,
                "error_code": str(result.error_code) if result.error_code else None,
                "latency_ms": result.latency_ms,
            }
        )
        return result


async def _model_rounds(visit: _Visit) -> LLMUsage:
    """Let the model pick tools, round by round, until it stops or the budget is used."""
    deps, state = visit.deps, visit.state
    tools = deps.registry.schemas_for(AGENT)
    messages: list[dict[str, Any]] = [{"role": "user", "content": _task(state, deps)}]
    usage = LLMUsage(model=deps.llm.model_for("fast"))
    for _ in range(MAX_ROUNDS):
        reply = await deps.llm.tool_round(
            system=RETRIEVAL_PROMPT.render(), messages=messages, tools=tools, tier="fast"
        )
        usage = usage + reply.usage
        if not reply.tool_calls:
            break
        messages.append(reply.assistant_message())
        for call in reply.tool_calls:
            if visit.budget <= 0:
                content = "Skipped: the tool-call budget for this request is used up."
            else:
                try:
                    arguments = json.loads(call.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = None
                if not isinstance(arguments, dict):
                    content = compact({"error": "invalid_input", "message": "Arguments not JSON"})
                else:
                    content = _for_model(await visit.call(call.name, arguments))
            # Every tool call must get an answer, or the next request is rejected.
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
        if visit.budget <= 0:
            break
    return usage


async def run(state: dict[str, Any], deps: AgentDeps) -> AgentOutcome:
    warnings: list[str] = []
    usage = LLMUsage()
    async with deps.session_factory() as session:
        visit = _Visit(state, deps, session)
        try:
            usage = await _model_rounds(visit)
            if not visit.log and not state.get("retrieved_data"):
                planned = fallback_calls(state, deps)
                if planned:
                    warnings.append(
                        "The model did not call any tools, so a standard set of records was "
                        "fetched instead. Check that LLM_MODEL_FAST supports tool calling "
                        "(python -m scripts.check_llm)."
                    )
                    for name, arguments in planned:
                        await visit.call(name, arguments)
        except LLMError as err:
            warnings.append(f"The model could not choose tools ({err.message}); used a fixed plan.")
            for name, arguments in fallback_calls(state, deps):
                if visit.budget > 0:
                    await visit.call(name, arguments)
    failed = [entry for entry in visit.log if entry["status"] != "success"]
    errors = [f"{entry['tool']} failed ({entry['error_code']})" for entry in failed]
    summary = f"{len(visit.log)} tool calls, {len(visit.log) - len(failed)} ok"
    if failed:
        summary += f"; failed: {', '.join(e['tool'] for e in failed)}"
    return AgentOutcome(
        update={
            "retrieved_data": visit.records,
            "tool_results": visit.log,
            "data_requests": [],
            "completed_steps": [AGENT],
            "warnings": warnings,
            "errors": errors,
        },
        summary=summary,
        usage=usage,
    )
