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

* he, him, his, she, her (Hinglish: usne, usko, uska, unka...; Hindi: उसने, उसका...):
  always someone or something specific, and nothing is named.
* it, its, they, them, their, this, that, these, those (Hinglish: woh, yeh, isko...;
  Hindi: वह, यह...): unless a word before it in the message can be what it points at
  ("Which products are low? Reorder any of them?").

Not pointing words: "this week", "woh din" (dates), and the "it" of "is it okay to ..."
or "is it true that ...", which points at nothing. Greetings and out-of-scope messages
are left alone ("How does this work?" points at ORM_AI itself). The question back is in
the shopkeeper's language (app/agents/language.py).
"""

import re
from dataclasses import dataclass

from app.agents.common import ids_in
from app.agents.language import DEVANAGARI_RANGE, Language
from app.agents.schemas import Entities

PERSON = frozenset(
    {"he", "him", "his", "himself", "she", "her", "hers", "herself"}
    # Hinglish and Hindi: he/she did, to him/her, his/her/their.
    | {"usne", "unhone", "usko", "unko", "unhe", "uska", "uski", "uske", "unka", "unki", "unke"}
    | {"उसने", "उन्होंने", "उसको", "उनको", "उन्हें", "उसका", "उसकी", "उसके", "उनका", "उनकी", "उनके"}
)
DEMONSTRATIVES = frozenset(
    {"this", "that", "these", "those", "woh", "wo", "voh", "vo", "yeh", "ye", "वह", "वो", "यह", "ये"}
)
POINTERS = DEMONSTRATIVES | frozenset(
    {"it", "its", "they", "them", "their"}
    | {"isko", "iska", "iski", "iske", "inka", "inki", "inke", "इसको", "इसका", "इसकी", "इसके"}
)
TIME_WORDS = frozenset(
    {"week", "weekend", "month", "year", "day", "morning", "afternoon", "evening", "night",
     "time", "season", "quarter", "festival",
     "din", "hafta", "hafte", "mahina", "mahine", "saal", "subah", "shaam", "raat", "baar",
     "दिन", "हफ्ता", "हफ्ते", "महीना", "महीने", "साल", "सुबह", "शाम", "रात", "बार"}
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
    # Hinglish: helpers, question words, small words, the speaker, request verbs.
    "kya hai hain tha thi ka ki ke ko se mein mai par pe aur ya nahi nahin na bhi toh "
    "abhi aaj kal phir ab kitna kitne kitni kab kaun kaunsa kaunsi kahan kaise kyun kyon "
    "mera meri mere hamara hamari apna apni apne aap aapka tum main hum mujhe humein bhai "
    "karo kar karna kardo kariye de dena dijiye bhejo bhej batao bataiye bata dikhao "
    "dekho wapas la laa laya laye lana sakte sakta sakti chahiye raha rahe rahi hua hui "
    "gaya gayi aaya aayi aa liya diya ho hoga hogi wala wali wale sirf bas zara thoda",
    # Hindi in Devanagari, the same kinds of words.
    "क्या है हैं था थी का की के को से में पर और या नहीं भी तो अभी आज कल फिर कितना कितने "
    "कितनी कब कौन कौनसा कहाँ कैसे क्यों मेरा मेरी हमारा आप मुझे करो कर करना दो दे दीजिए "
    "भेजो बताओ बताइए वापस ला लाया सकते सकता चाहिए रहा हुआ गया आया हो वाला वाली",
)
FUNCTION = PERSON | POINTERS | frozenset(w for group in _FUNCTION_GROUPS for w in group.split())
DUMMY_IT_COPULA = frozenset({"is", "was", "isn", "wasn", "would", "will", "s", "seems", "looks"})
DUMMY_IT_FOLLOWERS = frozenset({"to", "if", "that", "whether"})

NOUN_KIND = {
    **dict.fromkeys(["order", "orders", "delivery", "deliveries", "shipment", "po"], "order"),
    **dict.fromkeys(["bill", "bills", "sale", "invoice", "receipt"], "bill"),
    **dict.fromkeys(["customer", "customers", "person", "man", "woman", "lady"], "customer"),
    **dict.fromkeys(["supplier", "suppliers", "vendor", "agency", "distributor"], "supplier"),
    **dict.fromkeys(["product", "products", "item", "items", "packet", "brand", "maal"], "product"),
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
MISSING = {
    "customer": "which customer",
    "supplier": "which supplier",
    "order": "which purchase order",
    "bill": "which bill",
    "product": "which product",
    "any": "what the request refers to",
}
QUESTIONS: dict[Language, dict[str, str]] = {
    "en": {
        "customer": (
            "Which customer do you mean? Please give their name or customer ID (like CUST-0001)."
        ),
        "supplier": (
            "Which supplier do you mean? Please give their name or supplier ID (like SUP-004)."
        ),
        "order": "Which purchase order do you mean? Please give its number (like PO-00585).",
        "bill": (
            "Which bill is it? Please give the bill number (like SALE-005668) or "
            "the customer's name."
        ),
        "product": "Which product do you mean? Please give its name or product ID (like PRD-0002).",
        "any": "Which product, customer, bill or order do you mean? Please give its name or ID.",
    },
    "hinglish": {
        "customer": (
            "Aap kis customer ki baat kar rahe hain? Unka naam ya customer ID "
            "(jaise CUST-0001) bataiye."
        ),
        "supplier": (
            "Aap kis supplier ki baat kar rahe hain? Unka naam ya supplier ID "
            "(jaise SUP-004) bataiye."
        ),
        "order": "Kaunsa purchase order? Uska number (jaise PO-00585) bataiye.",
        "bill": "Kaunsa bill? Bill number (jaise SALE-005668) ya customer ka naam bataiye.",
        "product": "Kaunsa product? Uska naam ya product ID (jaise PRD-0002) bataiye.",
        "any": (
            "Aap kis product, customer, bill ya order ki baat kar rahe hain? Naam ya ID bataiye."
        ),
    },
    "hi": {
        "customer": ("आप किस ग्राहक की बात कर रहे हैं? उनका नाम या ग्राहक ID (जैसे CUST-0001) बताइए।"),
        "supplier": ("आप किस सप्लायर की बात कर रहे हैं? उनका नाम या सप्लायर ID (जैसे SUP-004) बताइए।"),
        "order": "कौन सा खरीद ऑर्डर? उसका नंबर (जैसे PO-00585) बताइए।",
        "bill": "कौन सा बिल? बिल नंबर (जैसे SALE-005668) या ग्राहक का नाम बताइए।",
        "product": "कौन सा सामान? उसका नाम या प्रोडक्ट ID (जैसे PRD-0002) बताइए।",
        "any": "आप किस सामान, ग्राहक, बिल या ऑर्डर की बात कर रहे हैं? नाम या ID बताइए।",
    },
}
SKIPPED_INTENTS = frozenset({"general_help", "out_of_scope"})

WORD = re.compile(f"[a-z]+|[{DEVANAGARI_RANGE}]+")


@dataclass(frozen=True)
class Unresolved:
    word: str  # the pointing word, as found
    missing: str  # for missing_information, e.g. "which bill"
    question: str  # the question to ask back, in the shopkeeper's language


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
    text: str, intent: str, entities: Entities, history: str, *, language: Language = "en"
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
        return Unresolved(word=word, missing=MISSING[kind], question=QUESTIONS[language][kind])
    return None
