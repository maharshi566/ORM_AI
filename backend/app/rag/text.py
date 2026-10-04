"""Text helpers shared by chunking, keyword search and the offline embedder."""

import math
import re
import unicodedata

# Common English words that carry no meaning for search.
_STOPWORD_LINES = (
    "a about above after again all also am an and any are as at be because been before",
    "being below between both but by can could did do does doing done down during each",
    "either few for from further get gets had has have having he her here hers him his",
    "how i if in into is it its itself just let may me might more most must my no nor",
    "not now of off on once only or other our ours out over own per same she should so",
    "some such than that the their theirs them then there these they this those through",
    "to too under until up us very was we were what when where which while who whom why",
    "will with would you your yours",
)
STOPWORDS = frozenset(word for line in _STOPWORD_LINES for word in line.split())

_DIGIT_COMMA = re.compile(r"(?<=\d),(?=\d)")
_TOKEN = re.compile(r"[a-z0-9]+")
# Zero-width space, non-joiner and joiner, word joiner, byte-order mark.
_ZERO_WIDTH = dict.fromkeys([0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF])


def estimate_tokens(text: str) -> int:
    """Approximate token count: about 4 characters per token for English text.

    Close enough for sizing chunks, and it needs no tokenizer download. The exact
    count only matters to the embedding API, whose limit (8,191 tokens) is far above
    our chunk size.
    """
    return math.ceil(len(text) / 4) if text else 0


def normalize_unicode(text: str) -> str:
    """NFC-normalise and drop invisible zero-width characters."""
    return unicodedata.normalize("NFC", text).translate(_ZERO_WIDTH)


def _stem(word: str) -> str:
    """A deliberately small suffix stripper: reminders -> reminder, deliveries -> delivery."""
    if word.isdigit() or len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    if word.endswith("es") and len(word) > 4 and word[-3] in "sxz":
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    if word.endswith("ing") and len(word) > 5:
        return word[:-3]
    if word.endswith("ed") and len(word) > 4:
        return word[:-2]
    return word


def search_terms(text: str, *, keep_stopwords: bool = False) -> list[str]:
    """Lower-case, stemmed words used by keyword search and the offline embedder.

    Thousands separators are removed first, so "Rs 3,000" and "3000" match.
    """
    text = _DIGIT_COMMA.sub("", normalize_unicode(text).lower())
    words = _TOKEN.findall(text)
    if not keep_stopwords:
        words = [w for w in words if w not in STOPWORDS]
    return [_stem(w) for w in words]
