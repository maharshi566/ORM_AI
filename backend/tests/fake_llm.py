"""A scripted stand-in for the language model, so the agent graph runs in tests.

``RuleBasedLLM`` implements the same two methods as the real client
(``structured`` and ``tool_round``) with simple keyword rules: it reads the same text
a real model would read and answers in the same schemas. Everything around it is
real: the graph, the tools on a seeded database, the knowledge search, the
checkpointer and the validator. Switches make it misbehave on purpose:

* ``fail``: agent names whose model call raises LLMError (the model is down);
* ``more_data``: the first N investigations ask for more records (a loop);
* ``bad_citations``: the first N replies cite a document that was never retrieved;
* ``no_tools``: the model answers in text instead of calling tools.
"""

import json
import re
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from app.agents.common import CITATION, ids_of_kind
from app.agents.schemas import FinalResponse, InvestigationResult, TriageResult, empty_entities
from app.services.chat_model import (
    LLMError,
    LLMUsage,
    StructuredReply,
    ToolCallRequest,
    ToolRoundReply,
)

USAGE = LLMUsage(model="fake-model", input_tokens=100, output_tokens=20, latency_ms=1.0, calls=1)

# (words, intent): the first match wins, so the more specific rules come first.
RULES: list[tuple[tuple[str, ...], str]] = [
    (("return", "refund"), "returns"),
    (("reminder", "remind"), "payment_reminder"),
    (("arrived", "delivery", "not arrived", "supplier", "purchase order"), "supplier_issue"),
    (("counted", "fewer", "less than the system", "count"), "stock_discrepancy"),
    (("margin", "priced", "price"), "pricing"),
    (("credit limit for",), "policy_question"),
    (("owe", "credit", "udhaar", "dues"), "customer_credit"),
    (("sales", "sold"), "sales_report"),
    (("running low", "reorder", "low on"), "reorder"),
    (("stock", "in stock"), "stock_status"),
    (("policy", "rule"), "policy_question"),
    (("hello", "what can you do", "help me"), "general_help"),
]
ROUTES = {
    "stock_status": ["data"],
    "reorder": ["data", "knowledge"],
    "sales_report": ["data"],
    "policy_question": ["knowledge"],
    "general_help": ["knowledge"],
    "out_of_scope": [],
}
CATEGORIES = {
    "reorder": "reorder",
    "supplier_issue": "supplier_issue",
    "stock_discrepancy": "stock_discrepancy",
    "pricing": "pricing",
    "returns": "returns",
    "customer_credit": "credit_dispute",
}


def classify(text: str) -> str:
    lowered = text.lower()
    for words, intent in RULES:
        if any(word in lowered for word in words):
            return intent
    return "out_of_scope"


def _line(text: str, prefix: str) -> str:
    match = re.search(rf"^{re.escape(prefix)}(.*)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _today(text: str) -> date:
    match = re.search(r"\((\d{4}-\d{2}-\d{2})\)", text)
    return date.fromisoformat(match.group(1)) if match else date(2026, 9, 30)


class RuleBasedLLM:
    def __init__(
        self,
        *,
        fail: Sequence[str] = (),
        more_data: int = 0,
        bad_citations: int = 0,
        no_tools: bool = False,
    ) -> None:
        self.fail = set(fail)
        self.more_data = more_data
        self.bad_citations = bad_citations
        self.no_tools = no_tools
        self.calls: list[str] = []

    def model_for(self, tier: str) -> str:
        return "fake-model"

    def _maybe_fail(self, agent: str) -> None:
        self.calls.append(agent)
        if agent in self.fail:
            raise LLMError(f"The fake model is down for {agent}.", kind="unavailable")

    # ----------------------------------------------------------- structured

    async def structured(
        self, schema, *, system: str, messages: Sequence[dict[str, Any]], tier: str
    ) -> StructuredReply:
        task = str(messages[-1]["content"])
        if schema is TriageResult:
            self._maybe_fail("triage")
            value = self._triage(task)
        elif schema is InvestigationResult:
            self._maybe_fail("investigation")
            value = self._investigation(task)
        elif schema is FinalResponse:
            self._maybe_fail("respond")
            value = self._response(task)
        else:
            raise AssertionError(f"unexpected schema {schema}")
        return StructuredReply(value=value, usage=USAGE, mode="json_schema")

    def _triage(self, task: str) -> TriageResult:
        request = task.splitlines()[-1]
        intent = classify(request)
        entities = empty_entities()
        for kind in (
            "product_ids",
            "customer_ids",
            "purchase_order_ids",
            "sale_ids",
            "supplier_ids",
        ):
            setattr(entities, kind, ids_of_kind(request, kind))
        unclear = intent == "payment_reminder" and not entities.customer_ids
        return TriageResult(
            intent=intent,
            category=CATEGORIES.get(intent, "general"),
            priority="normal",
            summary=request,
            entities=entities,
            missing_information=["which customer"] if unclear else [],
            needs_clarification=unclear,
            clarifying_question="Which customer should I remind?" if unclear else None,
            wants_action=intent in {"payment_reminder"},
            recommended_route=ROUTES.get(intent, ["data", "knowledge"]),
            confidence=0.9,
        )

    def _investigation(self, task: str) -> InvestigationResult:
        request = _line(task, "Request: ")
        intent = classify(request)
        sources = re.findall(r'<record source="([^"]+)"', task)
        citations = list(dict.fromkeys(CITATION.findall(task)))
        if self.more_data > 0:
            self.more_data -= 1
            return InvestigationResult(
                issue_type="not sure yet",
                summary="The stock movements are needed first.",
                findings=[],
                evidence=[],
                policy_references=[],
                recommended_action="Fetch more records.",
                proposed_actions=[],
                confidence=0.4,
                requires_human_review=False,
                needs_more_data=True,
                data_requests=["stock movements for the product over 30 days"],
            )
        actions = []
        po_ids = ids_of_kind(request, "purchase_order_ids")
        customers = ids_of_kind(request, "customer_ids")
        if intent == "supplier_issue" and po_ids:
            issue = (
                "short"
                if any(w in request.lower() for w in ("only", "instead", "short"))
                else "late"
            )
            actions.append(
                {
                    "tool": "follow_up_supplier",
                    "arguments_json": json.dumps({"purchase_order_id": po_ids[0], "issue": issue}),
                    "reason": "POL-SUPPLIER-001 says to message the supplier.",
                }
            )
        if intent == "payment_reminder" and customers:
            actions.append(
                {
                    "tool": "send_payment_reminder",
                    "arguments_json": json.dumps({"customer_id": customers[0]}),
                    "reason": "Dues are past due (POL-REMINDER-001).",
                }
            )
        evidence = [{"source": "record", "reference": s, "fact": "Checked."} for s in sources[:2]]
        evidence += [
            {"source": "policy", "reference": c, "fact": "Applies."} for c in citations[:1]
        ]
        return InvestigationResult(
            issue_type=intent,
            summary="Worked out from the records and the shop rules.",
            findings=[f"{s} was checked" for s in sources[:3]],
            evidence=evidence,
            policy_references=citations[:2],
            recommended_action="Follow the shop rule cited.",
            proposed_actions=actions,
            confidence=0.8,
            requires_human_review=bool(actions),
            needs_more_data=False,
            data_requests=[],
        )

    def _response(self, task: str) -> FinalResponse:
        request = _line(task, "Request: ")
        citations = list(dict.fromkeys(CITATION.findall(task)))[:2]
        sources = re.findall(r'<record source="([^"]+)"', task)
        pending = re.findall(r"^- (\w+ \(.*\): .*)$", task.split("NOT done", 1)[-1], re.MULTILINE)
        if "NOT done" not in task:
            pending = []
        answer = f"Here is what I found about: {request}"
        if citations:
            answer += f" The rule is {citations[0]}."
        if self.bad_citations > 0:
            self.bad_citations -= 1
            answer += " See [POL-MADEUP-999 v1 §1. Not real]."
        return FinalResponse(
            answer=answer,
            facts=[f"Checked {s}" for s in sources[:4]],
            evidence=[f"Applies: {c}" for c in citations],
            next_steps=["Review the details above."],
            pending_approval=pending,
            citations=citations,
            follow_up_question=None,
        )

    # ----------------------------------------------------------- tool calls

    async def tool_round(
        self,
        *,
        system: str,
        messages: Sequence[dict[str, Any]],
        tools: list[dict[str, Any]],
        tier: str,
    ) -> ToolRoundReply:
        self._maybe_fail("data_retrieval")
        if self.no_tools:
            return ToolRoundReply(content="I would look up the records.", usage=USAGE)
        if any(m.get("role") == "tool" for m in messages):
            return ToolRoundReply(content="Fetched what was needed.", usage=USAGE)
        task = str(messages[0]["content"])
        calls = [
            ToolCallRequest(id=f"call_{i}", name=name, arguments=json.dumps(args))
            for i, (name, args) in enumerate(self._plan(task))
        ]
        return ToolRoundReply(content=None, tool_calls=calls, usage=USAGE)

    @staticmethod
    def _plan(task: str) -> list[tuple[str, dict[str, Any]]]:
        request = _line(task, "Request: ")
        intent = classify(request)
        calls: list[tuple[str, dict[str, Any]]] = []
        products = ids_of_kind(request, "product_ids")
        for product_id in products:
            calls.append(("get_product", {"product_id": product_id}))
            if intent == "stock_discrepancy":
                calls.append(("get_stock_movements", {"product_id": product_id, "days": 30}))
            if intent == "pricing":
                calls.append(("check_supplier_price", {"product_id": product_id}))
        for customer_id in ids_of_kind(request, "customer_ids"):
            calls.append(("get_customer_account", {"customer_id": customer_id}))
        for sale_id in ids_of_kind(request, "sale_ids"):
            calls.append(("get_sale", {"sale_id": sale_id}))
        if ids_of_kind(request, "purchase_order_ids") or intent == "supplier_issue":
            calls.append(("get_purchase_orders", {}))
        if intent in {"reorder", "stock_status"} and not products:
            calls.append(("get_low_stock_products", {}))
        if intent == "sales_report":
            today = _today(task)
            start = today - timedelta(days=6)
            calls.append(
                (
                    "get_sales_summary",
                    {"start_date": start.isoformat(), "end_date": today.isoformat()},
                )
            )
        if "investigation agent needs" in task:  # a second visit: fetch the movements
            for product_id in ids_of_kind(task, "product_ids")[:1]:
                calls.append(("get_stock_movements", {"product_id": product_id, "days": 30}))
        return calls
