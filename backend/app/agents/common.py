"""Small helpers the agents share: what an agent returns, and how evidence is shown.

Every agent turns the same state into the same kind of text for its model, so the
formatting lives here once: earlier messages, the records fetched (labelled by the
tool call that returned them) and the policy passages (wrapped as untrusted data, with
citations). The ID and citation patterns are also here, because the agents and the
validator use them to check that an answer only mentions what the tools returned.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.rag.context import build_context
from app.services.chat_model import LLMUsage
from app.tools.knowledge_tools import KnowledgePassage

# Record IDs as the shop system writes them (see app/seed/generator.py).
ID_PATTERNS: dict[str, str] = {
    "product_ids": r"PRD-\d{4}",
    "customer_ids": r"CUST-\d{4}",
    "supplier_ids": r"SUP-\d{3}",
    "purchase_order_ids": r"PO-\d{5}",
    "sale_ids": r"SALE-\d{6}",
    "case_ids": r"CASE-\d{4}",
}
ANY_ID = re.compile(r"\b(?:" + "|".join(ID_PATTERNS.values()) + r")\b", re.IGNORECASE)
CITATION = re.compile(r"\[[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+ v\d+ §[^\[\]\n]+\]")

MAX_RECORD_CHARS = 6000  # per tool result shown to a model
MAX_HISTORY_CHARS = 1500  # per earlier message


@dataclass
class AgentOutcome:
    """What an agent hands back to its graph node."""

    update: dict[str, Any]
    summary: str  # one line for the trace and the agent_runs table, never hidden reasoning
    usage: LLMUsage = field(default_factory=LLMUsage)


def ids_in(text: str) -> set[str]:
    return {match.upper() for match in ANY_ID.findall(text or "")}


def ids_of_kind(text: str, kind: str) -> list[str]:
    found = re.findall(rf"\b{ID_PATTERNS[kind]}\b", text or "", re.IGNORECASE)
    return list(dict.fromkeys(match.upper() for match in found))


def citations_in(text: str) -> list[str]:
    return list(dict.fromkeys(CITATION.findall(text or "")))


def compact(value: Any, limit: int = MAX_RECORD_CHARS) -> str:
    text = json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


def record_key(tool: str, arguments: dict[str, Any]) -> str:
    """A stable name for one tool call, e.g. 'get_product(product_id=PRD-0002)'."""
    parts = ", ".join(f"{k}={arguments[k]}" for k in sorted(arguments) if arguments[k] is not None)
    return f"{tool}({parts})"


def today_line(now: datetime) -> str:
    return f"Today is {now:%A, %d %B %Y} ({now.date().isoformat()})."


def history_messages(state: dict[str, Any]) -> list[dict[str, str]]:
    """Earlier turns of the conversation, as chat messages (short-term memory)."""
    messages = []
    for turn in state.get("conversation_history") or []:
        role = turn.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = str(turn.get("content") or "")[:MAX_HISTORY_CHARS]
        if content:
            messages.append({"role": role, "content": content})
    return messages


def history_text(state: dict[str, Any]) -> str:
    return "\n".join(str(t.get("content") or "") for t in state.get("conversation_history") or [])


def render_records(state: dict[str, Any]) -> str:
    """The records fetched so far, one block per tool call, for a model to read."""
    records = state.get("retrieved_data") or {}
    if not records:
        return "<records>\nNo records were fetched.\n</records>"
    blocks = []
    for key, record in records.items():
        if record.get("status") == "success":
            blocks.append(f'<record source="{key}">\n{compact(record.get("data"))}\n</record>')
        else:
            blocks.append(
                f'<record source="{key}" status="error">\n'
                f"{record.get('error_code')}: {record.get('error_message')}\n</record>"
            )
    return "<records>\n" + "\n".join(blocks) + "\n</records>"


def render_passages(state: dict[str, Any], max_tokens: int = 2500) -> str:
    passages = [KnowledgePassage.model_validate(p) for p in state.get("retrieved_documents") or []]
    return build_context(passages, max_tokens=max_tokens)  # type: ignore[arg-type]


def allowed_citations(state: dict[str, Any]) -> set[str]:
    return {p["citation"] for p in state.get("retrieved_documents") or []}


def known_ids(state: dict[str, Any]) -> set[str]:
    """IDs the user wrote or a tool returned: the only IDs an answer may mention."""
    text = " ".join(
        [
            state.get("user_query") or "",
            history_text(state),
            compact(state.get("retrieved_data") or {}, limit=10**7),
        ]
    )
    return ids_in(text)


def request_header(state: dict[str, Any], now: datetime) -> str:
    triage = state.get("triage") or {}
    lines = [
        f"Shop: {state.get('shop_id')}",
        today_line(now),
        f"Request: {state.get('user_query')}",
    ]
    if triage:
        lines.append(f"Triage: intent={triage.get('intent')}, summary={triage.get('summary')}")
        entities = {k: v for k, v in (state.get("entities") or {}).items() if v}
        if entities:
            lines.append(f"Entities: {compact(entities, 1500)}")
    return "\n".join(lines)
