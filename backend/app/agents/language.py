"""Which language the shopkeeper wrote in, and the fixed text in that language.

Shopkeepers write English, Hindi (Devanagari), or very often Hinglish: Hindi in Latin
letters, mixed with English words ("PRD-0002 ka stock kitna hai?"). ORM_AI answers in
the same language. The models write the free text; this module decides the language
in code (so every agent and the evaluation agree on it) and holds the text ORM_AI
writes itself: the reply's section titles, the fixed replies, and the clarifying
questions (app/agents/references.py).

``detect_language`` is deliberately simple: Devanagari letters mean Hindi; otherwise
two or more common Hindi words written in Latin letters mean Hinglish; otherwise
English. One Hindi word in an English sentence ("customer's udhaar") stays English.
"""

import re
from typing import Literal

Language = Literal["en", "hi", "hinglish"]

# Devanagari letters, U+0900 to U+097F (written with chr() to keep the source plain).
DEVANAGARI_RANGE = f"{chr(0x0900)}-{chr(0x097F)}"
DEVANAGARI = re.compile(f"[{DEVANAGARI_RANGE}]")
LATIN_WORD = re.compile(r"[a-z]+")
# Common Hindi words in Latin letters that are not also English words.
HINGLISH_WORDS = frozenset(
    word
    for line in (
        "hai hain tha thi kya kab kaun kaunsa kaunsi kahan kaise kyun kyon kitna kitne kitni",
        "ka ki ke ko se mein mai mujhe hum humein hamara hamari mera meri mere apna apni apne",
        "aap aapka aapki aapke tum tumhara yeh ye woh wo voh usne unhone uska uski unka unki usko",
        "unko isko iska iski inka nahi nahin bhi aur ya toh abhi aaj kal parso phir pehle",
        "pichle pichli agle karo kar karna kardo karke kiya kiye diya diye dena dijiye bhejo",
        "bhej bheja bhejna batao bataiye bata dikhao dikhaiye dekho wapas laya laye lana",
        "sakte sakta sakti chahiye raha rahe rahi hua hui gaya gayi aaya aayi liya lena",
        "udhaar udhar baaki bakaya maal dukaan dukan paisa paise rupaye kitnaa jaldi theek",
        "accha acha haan bhai ji namaste shukriya dhanyavad zaroorat zarurat bina pooche",
        "puche maaf maafi khatam",
    )
    for word in line.split()
)

SECTION_TITLES: dict[Language, dict[str, str]] = {
    "en": {
        "facts": "From your records",
        "evidence": "Shop rules that apply",
        "next_steps": "Suggested next steps",
        "pending_approval": "Proposed, not done yet (needs your approval)",
        "completed_actions": "Done",
        "unverified": "Note: parts of this answer could not be fully checked against your records.",
    },
    "hinglish": {
        "facts": "Aapke records se",
        "evidence": "Dukaan ke niyam jo lagu hote hain",
        "next_steps": "Aage kya karein",
        "pending_approval": "Prastavit, abhi kiya nahi gaya (aapki approval chahiye)",
        "completed_actions": "Ho gaya",
        "unverified": (
            "Note: is jawab ke kuch hisse aapke records se poori tarah check nahi ho paaye."
        ),
    },
    "hi": {
        "facts": "आपके रिकॉर्ड से",
        "evidence": "दुकान के नियम जो लागू होते हैं",
        "next_steps": "आगे क्या करें",
        "pending_approval": "प्रस्तावित, अभी किया नहीं गया (आपकी मंज़ूरी चाहिए)",
        "completed_actions": "हो गया",
        "unverified": "नोट: इस जवाब के कुछ हिस्से आपके रिकॉर्ड से पूरी तरह जाँचे नहीं जा सके।",
    },
}

OUT_OF_SCOPE: dict[Language, str] = {
    "en": (
        "I can help with this shop's stock, sales, purchase orders, suppliers, customer "
        "credit (udhaar) and shop rules. Could you ask me something about those?"
    ),
    "hinglish": (
        "Main is dukaan ke stock, sales, purchase orders, suppliers, customers ke udhaar "
        "aur dukaan ke niyamon mein madad kar sakta hoon. Inmein se kuch poochiye?"
    ),
    "hi": (
        "मैं इस दुकान के स्टॉक, बिक्री, खरीद ऑर्डर, सप्लायर, ग्राहकों के उधार और दुकान के "
        "नियमों में मदद कर सकता हूँ। इनमें से कुछ पूछिए?"
    ),
}

LANGUAGE_NAMES: dict[Language, str] = {
    "en": "English",
    "hi": "Hindi, in Devanagari script",
    "hinglish": "Hinglish (Hindi written in Latin letters, mixed with English words)",
}


def detect_language(text: str | None) -> Language:
    text = text or ""
    letters = [ch for ch in text if ch.isalpha()]
    if letters and sum(1 for ch in letters if DEVANAGARI.match(ch)) / len(letters) >= 0.3:
        return "hi"
    words = LATIN_WORD.findall(text.lower())
    hindi = sum(1 for w in words if w in HINGLISH_WORDS)
    return "hinglish" if hindi >= 2 else "en"


def language_of(state: dict) -> Language:
    """The language triage recorded for this request (English when unknown)."""
    value = state.get("language")
    return value if value in SECTION_TITLES else "en"
