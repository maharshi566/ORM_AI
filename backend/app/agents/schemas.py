"""What each LLM agent must return: Pydantic models sent to the model as JSON schemas.

The models are written for OpenAI's strict structured-output mode, which every
field must satisfy: every field is required (optional ones are ``X | None``), there
are no free-form dictionaries, and limits such as "between 0 and 1" are enforced in
code after parsing rather than in the schema. Field descriptions are part of the
prompt: the model reads them.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.tools.registry import ACTION_TOOL_NAMES

Intent = Literal[
    "stock_status",  # what is in stock, product details, what is running low
    "reorder",  # what to order, whether to place a purchase order
    "sales_report",  # sales totals, best sellers, payment modes
    "customer_credit",  # what a customer owes (udhaar), credit limits, giving credit
    "payment_reminder",  # reminding a customer to pay
    "supplier_issue",  # late or short deliveries, supplier terms
    "stock_discrepancy",  # counted stock does not match the records, damage
    "pricing",  # margins, selling price, MRP, price changes
    "returns",  # customer returns and refunds
    "policy_question",  # what a shop rule or procedure says
    "general_help",  # greetings, what ORM_AI can do, how to use it
    "out_of_scope",  # anything not about running this shop
]
INTENTS: tuple[str, ...] = Intent.__args__  # type: ignore[attr-defined]

Category = Literal[
    "stock_discrepancy",
    "supplier_issue",
    "credit_dispute",
    "pricing",
    "customer_complaint",
    "reorder",
    "returns",
    "general",
]

# Built from the registry so a new action tool is offered without editing this file.
ActionToolName = Literal[tuple(ACTION_TOOL_NAMES)]  # type: ignore[valid-type]


def _unit_interval(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


class Entities(BaseModel):
    product_names: list[str] = Field(description="Products named in the request, as written")
    product_ids: list[str] = Field(description="Product IDs such as PRD-0002")
    customer_names: list[str]
    customer_ids: list[str] = Field(description="Customer IDs such as CUST-0001")
    supplier_names: list[str]
    supplier_ids: list[str] = Field(description="Supplier IDs such as SUP-004")
    purchase_order_ids: list[str] = Field(description="Purchase order IDs such as PO-00585")
    sale_ids: list[str] = Field(description="Bill IDs such as SALE-005668")
    case_ids: list[str] = Field(description="Case IDs such as CASE-0012")
    quantities: list[int]
    amounts: list[float] = Field(description="Rupee amounts mentioned")
    date_from: str | None = Field(description="Start of a date range, YYYY-MM-DD")
    date_to: str | None = Field(description="End of a date range, YYYY-MM-DD")


def empty_entities() -> Entities:
    return Entities(
        product_names=[],
        product_ids=[],
        customer_names=[],
        customer_ids=[],
        supplier_names=[],
        supplier_ids=[],
        purchase_order_ids=[],
        sale_ids=[],
        case_ids=[],
        quantities=[],
        amounts=[],
        date_from=None,
        date_to=None,
    )


class TriageResult(BaseModel):
    intent: Intent
    category: Category
    priority: Literal["low", "normal", "high", "urgent"]
    summary: str = Field(description="The request restated in one plain sentence")
    entities: Entities
    missing_information: list[str] = Field(
        description="Facts needed to answer that the request does not give, if any"
    )
    needs_clarification: bool = Field(
        description="True only when the request cannot be answered without asking first"
    )
    clarifying_question: str | None = Field(description="The question to ask, if any")
    wants_action: bool = Field(
        description="True when the user asks ORM_AI to do or change something "
        "(order, remind, adjust, refund, message), not just to look something up"
    )
    recommended_route: list[Literal["data", "knowledge"]] = Field(
        description="data: the shop's records are needed. knowledge: shop rules are needed."
    )
    confidence: float = Field(description="0 to 1: how sure the classification is")

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return _unit_interval(value)


class Evidence(BaseModel):
    source: Literal["record", "policy", "user"]
    reference: str = Field(
        description="For a record: the tool and ID, e.g. 'get_customer_account CUST-0001'. "
        "For a policy: its exact citation, e.g. '[POL-CREDIT-001 v2 §2. Credit limits]'."
    )
    fact: str = Field(description="The fact, in one short sentence")


class ProposedAction(BaseModel):
    tool: ActionToolName  # type: ignore[valid-type]
    arguments_json: str = Field(
        description="The tool's arguments as a JSON object, without idempotency_key"
    )
    reason: str = Field(description="Why, in one sentence, naming the rule it follows")


class InvestigationResult(BaseModel):
    issue_type: str = Field(description="Short label, e.g. 'over credit limit', 'late delivery'")
    summary: str = Field(description="The conclusion in one or two sentences")
    findings: list[str] = Field(description="Each a fact from the records, with its ID")
    evidence: list[Evidence]
    policy_references: list[str] = Field(description="Exact citations of the rules applied")
    recommended_action: str = Field(description="What the shopkeeper should do next")
    proposed_actions: list[ProposedAction] = Field(
        description="Actions ORM_AI could take with its tools. They are only proposed: "
        "a person approves them first. Empty when nothing should be done."
    )
    confidence: float = Field(description="0 to 1")
    requires_human_review: bool = Field(
        description="True for money, credit, price or stock changes, policy exceptions, "
        "disputes, conflicting rules, or low confidence"
    )
    needs_more_data: bool = Field(
        description="True only if a specific record that was not fetched is needed"
    )
    data_requests: list[str] = Field(
        description="If needs_more_data: what to fetch, e.g. 'stock movements for PRD-0002'"
    )

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return _unit_interval(value)


class FinalResponse(BaseModel):
    answer: str = Field(
        description="The direct answer in 1-4 short sentences of plain language, with "
        "citations in square brackets for any shop rule used"
    )
    facts: list[str] = Field(description="Facts from the shop's records, each with its ID")
    evidence: list[str] = Field(description="Shop rules that apply, each with its citation")
    next_steps: list[str] = Field(description="What the shopkeeper can do next")
    pending_approval: list[str] = Field(
        description="Proposed actions waiting for a person's approval, in plain words"
    )
    citations: list[str] = Field(description="Every citation used, exactly as given")
    follow_up_question: str | None = Field(description="A question back to the user, if any")
