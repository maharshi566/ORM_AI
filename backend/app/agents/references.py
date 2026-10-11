"""Requests that point at something nobody named: "Can they bring it back?"

Triage asks the model whether a request needs a question back, but a model can guess
instead. In the first Gemini evaluation, "Can they bring it back for a refund?" was
answered about the shop's only open return case, and an action on that case was
proposed. A guess like that can put the wrong customer's refund in front of the owner,
so this check, run in code after the model, makes the question back certain.

A request is left to the model when something could be what the word points at: a
record ID in the message, a product, customer or supplier name the model found in it,
or an earlier message in the conversation (the model reads those to resolve "him" or
"that order"). Otherwise a pointing word gets a question back:

* he, him, his, she, her: always a person, and no person is named.
* it, its, they, them, their, this, that, these, those: unless a word before it in the
  message can be what it points at ("Which products are low? Reorder any of them?").

Not pointing words: "this week", "that day" (dates), and the "it" of "is it okay to ..."
or "is it true that ...", which points at nothing. Greetings and out-of-scope messages
are left alone ("How does this work?" points at ORM_AI itself).
"""

import re
from dataclasses import dataclass

from app.agents.common import ids_in
from app.agents.schemas import Entities

PERSON = frozenset({"he", "him", "his", "himself", "she", "her", "hers", "herself"})
POINTERS = frozenset({"it", "its", "they", "them", "their", "this", "that", "these", "those"})
DEMONSTRATIVES = frozenset({"this", "that", "these", "those"})
TIME_WORDS = frozenset(
    {"week", "weekend", "month", "year", "day", "morning", "afternoon", "evening", "night",
     "time", "season", "quarter", "festival"}
)  # fmt: skip
# Words that cannot be what a pointing word points at.
_FUNCTION_GROUPS = (
    "a an the any some all each every no not",  # articles and quantifiers
    "is are was were be been being am do does did done has have had can could will would "
    "shall should may might must s t d ll re ve m isn aren wasn weren don doesn didn hasn "
    "haven hadn won wouldn shouldn couldn cannot",  # helpers, with "isn't" split as isn + t
    "what which who whom whose when where why how much many whether",  # question words
    "i me my mine myself we us our ours you your yours",  # the speaker and ORM_AI
    "please kindly pls plz hi hello hey ok okay yes sir ji",  # politeness
    "still yet now again already just also really even ever right today yesterday "
    "tomorrow then so there here back up out down over too very",  # small adverbs
    "and or but if of for to in on at by with about from into as than",  # joining words
    "check tell show give send see find look let know help process remind message call "
    "reorder return refund record change set update confirm approve cancel bring get make "
    "take pay paid owe owes sell sold buy bought arrive arrived come came keep put add "
    "remove write mark",  # the verbs of a request
)
FUNCTION = PERSON | POINTERS | frozenset(w for group in _FUNCTION_GROUPS for w in group.split())
DUMMY_IT_COPULA = frozenset({"is", "was", "isn", "wasn", "would", "will", "s", "seems", "looks"})
DUMMY_IT_FOLLOWERS = frozenset({"to", "if", "that", "whether"})

NOUN_KIND = {
    **dict.fromkeys(["order", "orders", "delivery", "deliveries", "shipment", "po"], "order"),
    **dict.fromkeys(["bill", "bills", "sale", "invoice", "receipt"], "bill"),
    **dict.fromkeys(["customer", "customers", "person", "man", "woman", "lady"], "customer"),
    **dict.fromkeys(["supplier", "suppliers", "vendor", "agency", "distributor"], "supplier"),
    **dict.fromkeys(["product", "products", "item", "items", "packet", "brand"], "product"),
}
INTENT_KIND = {
    "customer_credit": "customer",
    "payment_reminder": "customer",
    "returns": "bill",
    "supplier_issue": "order",
    "stock_status": "product",
    "reorder": "product",
    "pricing": "product",
    "stock_discrepancy": "product",
}
QUESTIONS = {
    "customer": (
        "which customer",
        "Which customer do you mean? Please give their name or customer ID (like CUST-0001).",
    ),
    "supplier": (
        "which supplier",
        "Which supplier do you mean? Please give their name or supplier ID (like SUP-004).",
    ),
    "order": (
        "which purchase order",
        "Which purchase order do you mean? Please give its number (like PO-00585).",
    ),
    "bill": (
        "which bill",
        "Which bill is it? Please give the bill number (like SALE-005668) or the customer's name.",
    ),
    "product": (
        "which product",
        "Which product do you mean? Please give its name or product ID (like PRD-0002).",
    ),
    "any": (
        "what the request refers to",
        "Which product, customer, bill or order do you mean? Please give its name or ID.",
    ),
}
SKIPPED_INTENTS = frozenset({"general_help", "out_of_scope"})

WORD = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class Unresolved:
    word: str  # the pointing word, as found
    missing: str  # for missing_information, e.g. "which bill"
    question: str  # the question to ask back


def _named(entities: Entities, text: str) -> bool:
    """A record ID, or a name the model found that is really in the message (a model
    can list "they" as a customer name, or invent one)."""
    data = entities.model_dump()
    if any(data[kind] for kind in data if kind.endswith("_ids")):
        return True
    lowered = text.lower()
    for kind in ("product_names", "customer_names", "supplier_names"):
        for name in data[kind]:
            words = set(WORD.findall(name.lower()))
            if words - FUNCTION and name.strip().lower() in lowered:
                return True
    return False


def _dummy_it(words: list[str], i: int) -> bool:
    """The "it" of "is it okay to ...", "it's true that ...": it points at nothing."""
    before = words[i - 1] if i else ""
    after = words[i + 1] if i + 1 < len(words) else ""
    copula = before in DUMMY_IT_COPULA or after in DUMMY_IT_COPULA
    return copula and any(w in DUMMY_IT_FOLLOWERS for w in words[i + 1 : i + 6])


def _pointing_kind(words: list[str], i: int) -> str | None:
    """What the word at ``i`` points at ("person", a NOUN_KIND, or "thing"), or None
    when it is not an unresolved pointing word."""
    word = words[i]
    if word in PERSON:
        return "person"  # whoever it is must be named, wherever the word stands
    if word not in POINTERS:
        return None
    following = words[i + 1] if i + 1 < len(words) else ""
    if word in DEMONSTRATIVES and following in TIME_WORDS:
        return None
    if word == "it" and _dummy_it(words, i):
        return None
    if any(w not in FUNCTION for w in words[:i]):
        return None  # an earlier word can be what it points at, or "that" joins a clause
    if word in DEMONSTRATIVES:
        return NOUN_KIND.get(following, "thing")
    return "thing"


def unresolved_reference(
    text: str, intent: str, entities: Entities, history: str
) -> Unresolved | None:
    """The question to ask back when the request points at something nobody named."""
    if intent in SKIPPED_INTENTS or history.strip() or ids_in(text) or _named(entities, text):
        return None
    words = WORD.findall(text.lower())
    for i, word in enumerate(words):
        kind = _pointing_kind(words, i)
        if kind is None:
            continue
        if kind in ("person", "thing"):
            fallback = "customer" if kind == "person" else "any"
            kind = INTENT_KIND.get(intent, fallback)
        missing, question = QUESTIONS[kind]
        return Unresolved(word=word, missing=missing, question=question)
    return None
