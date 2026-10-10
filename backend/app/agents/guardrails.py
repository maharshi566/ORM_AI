"""Guardrails: plain-Python checks on what comes in and what goes out.

Three jobs, all in code so they cannot be talked out of a rule:

* **Input check** (``check_input``), run by the triage agent on every message: removes
  control characters, caps the length, and flags text that tries to change ORM_AI's
  rules ("ignore your instructions", "approve this without asking the owner"). A
  flagged message is still answered as an ordinary request, but every action it leads
  to needs the owner's approval, and the reply is told not to give in.
* **Injection in documents** (``injection_sentences``): finds the instruction-like
  sentences in untrusted passages (supplier flyers). The validator blocks a reply that
  does what such a sentence says (``complies_with_injection``).
* **Personal data in logs** (``mask_pii``): phone numbers and email addresses are
  replaced before a line is written (used by app/core/logging.py).

Patterns are deliberately simple and readable. They catch the common phrasings; the
real protection is structural: tools check approvals themselves, and passages are
always wrapped as untrusted data.
"""

import re
from dataclasses import dataclass, field

MAX_MESSAGE_CHARS = 2000

# Text that tries to change how ORM_AI behaves rather than ask about the shop.
INJECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "override_instructions": re.compile(
        r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}"
        r"\b(?:instructions?|rules?|polic(?:y|ies)|guidelines?|prompts?|approvals?)\b",
        re.IGNORECASE,
    ),
    "skip_approval": re.compile(
        r"\b(?:without|skip(?:ping)?|bypass(?:ing)?|no need for|don'?t need)\b[^.\n]{0,25}"
        r"\b(?:approv\w*|permission|owner'?s? (?:ok|consent))\b"
        r"|\bapprove\b[^.\n]{0,40}\b(?:automatically|all|every)\b",
        re.IGNORECASE,
    ),
    "hide_from_owner": re.compile(
        r"\b(?:do not|don'?t|never|without)\s+(?:tell|telling|inform|informing|notify|"
        r"notifying|show|showing)\b[^.\n]{0,20}\b(?:owner|shop ?keeper|anyone|staff)\b",
        re.IGNORECASE,
    ),
    "new_role": re.compile(
        r"\byou are (?:now|no longer)\b|\bact as (?:an?|the) (?:admin|owner|developer|system)\b"
        r"|\b(?:developer|god|jailbreak|dan) mode\b",
        re.IGNORECASE,
    ),
    "reveal_prompt": re.compile(
        r"\b(?:reveal|show|print|repeat|tell me)\b[^.\n]{0,20}"
        r"\b(?:system prompt|your (?:prompt|instructions|rules))\b",
        re.IGNORECASE,
    ),
    "fake_system_message": re.compile(
        r"\b(?:system (?:notice|message|override)|important system notice)\b"
        r"|^\s*(?:system|assistant)\s*:",
        re.IGNORECASE | re.MULTILINE,
    ),
    "mass_change": re.compile(
        r"\b(?:mark|set|make)\b[^.\n]{0,30}\b(?:all|every)\b[^.\n]{0,40}"
        r"\b(?:paid|cleared|zero|approved|written off)\b",
        re.IGNORECASE,
    ),
}

FLAG_REASONS = {
    "override_instructions": "asks ORM_AI to ignore its rules",
    "skip_approval": "asks for actions without the required approval",
    "hide_from_owner": "asks to keep something from the owner",
    "new_role": "tries to give ORM_AI a different role",
    "reveal_prompt": "asks for ORM_AI's hidden instructions",
    "fake_system_message": "pretends to be a system message",
    "mass_change": "asks to change many records at once",
}

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")


@dataclass(frozen=True)
class InputCheck:
    text: str  # the message as the agents should read it
    flags: list[str] = field(default_factory=list)  # keys of INJECTION_PATTERNS that matched
    truncated: bool = False

    @property
    def flagged(self) -> bool:
        return bool(self.flags)

    def reasons(self) -> list[str]:
        return [FLAG_REASONS[flag] for flag in self.flags]


def injection_flags(text: str) -> list[str]:
    return [name for name, pattern in INJECTION_PATTERNS.items() if pattern.search(text or "")]


def check_input(text: str, *, max_chars: int = MAX_MESSAGE_CHARS) -> InputCheck:
    cleaned = CONTROL_CHARS.sub("", text or "").strip()
    truncated = len(cleaned) > max_chars
    if truncated:
        cleaned = cleaned[:max_chars]
    return InputCheck(text=cleaned, flags=injection_flags(cleaned), truncated=truncated)


# ------------------------------------------------------------ untrusted documents

SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
# Words that show a sentence refuses an instruction instead of following it. Reporting
# verbs ("the flyer says") are not enough: "as the notice says, I marked all balances as
# paid" is compliance. A report usually uses the bare verb ("says to mark"), which the
# compliance pattern below does not match anyway.
DISTANCING = re.compile(
    r"\b(?:not|never|won'?t|cannot|can'?t|didn'?t|refus\w*|ignor\w*|instead|rather than)\b",
    re.IGNORECASE,
)
# Doing what an injected instruction says, in a reply.
COMPLIANCE = re.compile(
    r"\b(?:marked|marking|cleared|clearing|set)\b[^.\n]{0,40}\b(?:paid|cleared|zero)\b"
    r"|\b(?:approved|approving|will approve|auto-?approved?)\b[^.\n]{0,40}"
    r"\b(?:automatically|all|every)\b"
    r"|\b(?:skipp?ed|skipping|bypass\w*|without)\b[^.\n]{0,25}\bapprov\w*",
    re.IGNORECASE,
)
# Keeping something from the owner is itself said with a negation, so it is checked
# without the refusal words.
HIDING = re.compile(
    r"\b(?:won'?t|will not|didn'?t|did not|have not|haven'?t)\s+(?:tell|told|inform|notify)"
    r"\w*\b[^.\n]{0,20}\bowner\b",
    re.IGNORECASE,
)


def injection_sentences(passages: list[dict]) -> list[str]:
    """Instruction-like sentences inside untrusted passages (supplier flyers)."""
    found: list[str] = []
    for passage in passages:
        if passage.get("trust") != "untrusted":
            continue
        for sentence in SENTENCE.findall(passage.get("text") or ""):
            if injection_flags(sentence):
                found.append(sentence.strip())
    return found


def complies_with_injection(text: str) -> list[str]:
    """Sentences of a reply that do what an injected instruction asked."""
    hits = []
    for sentence in SENTENCE.findall(text or ""):
        complied = COMPLIANCE.search(sentence) and not DISTANCING.search(sentence)
        if complied or HIDING.search(sentence):
            hits.append(sentence.strip())
    return hits


# ------------------------------------------------------------ personal data in logs

PHONE = re.compile(r"(?<![\w-])(?:\+?91[\s-]?)?(?:\d[\s-]?){9}\d(?![\w-])")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Keys whose whole value is personal data.
PII_KEY_PARTS = ("phone", "mobile", "email", "address", "recipient", "sent_to")


def mask_pii(text: str) -> str:
    """Phone numbers and email addresses replaced by [phone] and [email]."""
    if not text:
        return text
    text = EMAIL.sub("[email]", text)
    return PHONE.sub("[phone]", text)


def is_pii_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in PII_KEY_PARTS)
