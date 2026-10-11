"""Phone numbers: one form for logins and WhatsApp, and a check before sending.

``normalize_phone`` turns what people type ("98765 43210", "+91-98765-43210",
"09876543210") into E.164 ("+919876543210"), the form WhatsApp and the ``users``
table use. A ten-digit number without a country code is taken as Indian.

``is_dialable`` decides whether a message may go to a number at all. Indian mobile
numbers start with 6, 7, 8 or 9; the synthetic data's numbers ("+91-00000-40001") do
not, so nothing can ever be sent to a made-up customer by mistake.
"""

import re

DEFAULT_COUNTRY_CODE = "91"
_SEPARATORS = re.compile(r"[\s\-().]")


def normalize_phone(raw: str | None, *, country_code: str = DEFAULT_COUNTRY_CODE) -> str | None:
    """The number in E.164, or None when it cannot be one."""
    if not raw:
        return None
    text = _SEPARATORS.sub("", raw.strip())
    if text.startswith("00"):
        text = "+" + text[2:]
    if text.startswith("+"):
        digits = text[1:]
    elif len(text) == 11 and text.startswith("0"):
        digits = country_code + text[1:]  # a trunk prefix: 09876543210
    elif len(text) == 10:
        digits = country_code + text
    else:
        digits = text
    if not digits.isdigit() or not 8 <= len(digits) <= 15 or digits.startswith("0"):
        return None
    return "+" + digits


def is_dialable(e164: str | None) -> bool:
    """True for a number a real message can go to (stricter for India, the home market)."""
    if not e164 or not re.fullmatch(r"\+[1-9]\d{7,14}", e164):
        return False
    if e164.startswith("+91"):
        return re.fullmatch(r"\+91[6-9]\d{9}", e164) is not None
    return True
