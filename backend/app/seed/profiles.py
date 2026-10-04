"""Shop-profile documents for the knowledge base.

SHOP-001 to SHOP-005 have hand-written profiles. The other forty-five are generated
here from the shop catalogue (``app/seed/catalog.py``), so the knowledge base and the
database always describe the same shops: the same owner, staff, locality and credit
limits. Everything is derived from the shop's ID, so a file only changes when the
catalogue does.

Write or refresh the files with ``python -m scripts.generate_shop_profiles``. Add
``--check`` to only compare (the test suite does that, so a profile can never drift
away from the catalogue).
"""

import json
import random
import re
import string
from pathlib import Path

from app.seed.catalog import CITY, CUSTOMER_MIX, SHOPS, ShopSpec

HAND_WRITTEN_SHOPS = 5  # SHOP-001 to SHOP-005 keep the profiles written by hand
EFFECTIVE_DATE = "2026-04-01"

TYPE_LABEL = {
    "kirana": "kirana (grocery and daily needs)",
    "hardware": "hardware, paint and plumbing",
    "stationery": "stationery, school supplies and photocopying",
    "dairy_bakery": "dairy and bakery",
    "mobile_accessories": "mobile phone accessories",
}

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

# Opening and closing times to choose from, per type of shop.
_TIMES: dict[str, tuple[list[str], list[str]]] = {
    "kirana": (["6:30 am", "7:00 am", "7:30 am"], ["9:00 pm", "9:30 pm", "10:00 pm"]),
    "dairy_bakery": (["5:30 am", "6:00 am", "6:30 am"], ["9:30 pm", "10:00 pm"]),
    "hardware": (["8:30 am", "9:00 am", "9:30 am"], ["7:30 pm", "8:00 pm", "8:30 pm"]),
    "stationery": (["8:30 am", "9:00 am", "9:30 am"], ["8:00 pm", "8:30 pm", "9:00 pm"]),
    "mobile_accessories": (
        ["10:00 am", "10:30 am", "11:00 am"],
        ["8:30 pm", "9:00 pm", "9:30 pm"],
    ),
}

# The closed day, with how common each choice is.
_OPEN_DAYS: dict[str, list[tuple[str, int]]] = {
    "kirana": [("every day", 9), ("closed on Tuesdays", 1)],
    "dairy_bakery": [("every day", 1)],
    "hardware": [("closed on Sundays", 7), ("closed on Mondays", 2), ("every day", 1)],
    "stationery": [("closed on Sundays", 7), ("every day", 3)],
    "mobile_accessories": [("every day", 8), ("closed on Mondays", 2)],
}

# Values for the {placeholders} in the house rules below.
_VALUES: dict[str, list[str]] = {
    "weekday": _WEEKDAYS,
    "km": ["1", "2", "3"],
    "amount": ["300", "400", "500"],
    "day": ["5th", "10th", "last day"],
    "rate": ["1", "2"],
}

# Three of these are chosen for each shop (the same three every time for the same shop).
_RULES: dict[str, list[str]] = {
    "kirana": [
        "Rice, atta and sugar are ordered together from Shree Balaji Traders every {weekday}.",
        "Cooking oil and pulses are counted every week because they are high-value and "
        "fast-moving.",
        "Eggs are ordered in trays of 30 along with the rice and flour order.",
        "Home delivery within {km} km for bills above Rs {amount}; no deliveries after 8:00 pm.",
        "Before Diwali and Pongal keep double stock of sugar, oil and dal.",
        "Packets are checked for best-before dates on the first day of every month.",
        "A UPI QR code is at the counter; cards are accepted for bills above Rs 500.",
    ],
    "hardware": [
        "Cement is the top seller. Keep at least 20 bags; order full truckloads of 80 bags "
        "from Deccan Hardware Distributors.",
        "Paint is ordered from Colorline Paints Depot. Demand rises before festivals "
        "(September to November).",
        "Small fittings and fasteners are counted every month because they go missing easily.",
        "Orders above Rs {amount} are delivered by tempo within {km} km; a driver charge "
        "applies beyond that.",
        "Electrical items are tested at the counter before the sale.",
        "Contractors' monthly accounts are settled on the {day} of each month.",
    ],
    "stationery": [
        "Copier paper is also used for the shop's own photocopying, so never let it run out.",
        "June (schools reopen) and February to March (exams) are the busy periods: order "
        "notebooks and pens a month early.",
        "Photocopies cost Rs {rate} a page.",
        "Calculators, geometry boxes and staplers are counted every month.",
        "Bulk orders from schools go out with a delivery challan and are billed at month end.",
    ],
    "dairy_bakery": [
        "Order milk and bread every evening for the next morning.",
        "Record expired items as damage at closing every day.",
        "Bakery items still unsold after two days are written off as damage.",
        "The chiller temperature is checked every morning before opening.",
        "Birthday cakes are made to order: take half the amount in advance and note it on "
        "the bill.",
        "Milk for monthly account customers is delivered within {km} km before 8:00 am.",
    ],
    "mobile_accessories": [
        "Test every charger, earbud and speaker in front of the customer before the sale.",
        "Defective items within 7 days are replaced from stock and returned to the supplier "
        "under warranty (POL-RETURNS-001).",
        "Screen guards and covers sell fastest: reorder when 20 pieces are left.",
        "A screen guard is fitted free when the customer buys it at the counter.",
        "Cables and chargers are counted every week because they are small and easy to lose.",
        "Warranty cards are stapled to the bill for earbuds, power banks and speakers.",
    ],
}


def _rupees(amount: int) -> str:
    return f"Rs {amount:,}"


def _credit_rule(spec: ShopSpec) -> str:
    """The credit rule that matches the customers the generator creates for this type."""
    mix = CUSTOMER_MIX[spec.shop_type]
    limits = {kind: limit for kind, _, credit_share, limit in mix if credit_share > 0}
    if spec.shop_type == "kirana":
        return (
            f"Credit only for families known to the owner, up to {_rupees(limits['household'])} "
            "(POL-CREDIT-001 v2)."
        )
    if spec.shop_type == "hardware":
        return (
            f"Registered contractors may buy on credit up to {_rupees(limits['contractor'])} "
            f"and households up to {_rupees(limits['household'])} (POL-CREDIT-001 v2)."
        )
    if spec.shop_type == "stationery":
        return (
            f"Schools and tuition centres may buy on credit up to "
            f"{_rupees(limits['institution'])} and pay monthly; families known to the owner "
            f"up to {_rupees(limits['household'])}. Students do not get credit."
        )
    if spec.shop_type == "dairy_bakery":
        return (
            f"Monthly milk accounts have a {_rupees(limits['household'])} limit and are "
            "settled by the 5th of each month."
        )
    return (
        f"Credit only for repair shops that buy regularly, up to {_rupees(limits['business'])} "
        "(POL-CREDIT-001 v2)."
    )


def _fill(template: str, rng: random.Random) -> str:
    names = [name for _, name, _, _ in string.Formatter().parse(template) if name]
    return template.format(**{name: rng.choice(_VALUES[name]) for name in names})


def _yaml_text(text: str) -> str:
    """A front-matter value: plain when that is safe, otherwise double-quoted."""
    if re.search(r": | #|^[&*!|>'\"%@`\[\]{},-]", text):
        return json.dumps(text, ensure_ascii=False)
    return text


def profile_slug(spec: ShopSpec) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", spec.name.lower().replace("'", ""))
    return words.strip("-")


def profile_path(spec: ShopSpec) -> str:
    """Where the profile lives, relative to the knowledge-base folder."""
    return f"shops/shop-{spec.id[-3:]}-{profile_slug(spec)}.md"


def render_profile(spec: ShopSpec) -> str:
    """The full Markdown file (front-matter and body) for one shop."""
    rng = random.Random(f"shop-profile-{spec.id}")  # a string seed is stable across runs
    opens, closes = _TIMES[spec.shop_type]
    days = _OPEN_DAYS[spec.shop_type]
    open_days = rng.choices([label for label, _ in days], weights=[w for _, w in days])[0]
    hours = f"{rng.choice(opens)} to {rng.choice(closes)}, {open_days}"

    pool = _RULES[spec.shop_type]
    chosen = sorted(rng.sample(range(len(pool)), 3))
    rules = [_credit_rule(spec), *(_fill(pool[index], rng) for index in chosen)]

    number = spec.id[-3:]
    lines = [
        "---",
        f"document_id: SHOP-PROFILE-{number}",
        f"title: {_yaml_text(f'Shop profile — {spec.name}')}",
        "source: internal",
        "category: shop_profile",
        "version: 1",
        f"effective_date: {EFFECTIVE_DATE}",
        "status: current",
        f"shop_id: {spec.id}",
        "---",
        "",
        f"# Shop profile: {spec.name} ({spec.id})",
        "",
        "## Basics",
        "",
        f"- Type: {TYPE_LABEL[spec.shop_type]}.",
        f"- Owner: {spec.owner_name}. Staff: {spec.staff_name}.",
        f"- Location: {spec.locality}, {CITY}.",
        f"- Hours: {hours}.",
        f"- Open since: {spec.opened_on.year}.",
        "",
        "## House rules",
        "",
        *(f"- {rule}" for rule in rules),
        "",
    ]
    return "\n".join(lines)


def generated_profiles() -> dict[str, str]:
    """Relative path -> file content, for every shop without a hand-written profile."""
    return {profile_path(spec): render_profile(spec) for spec in SHOPS[HAND_WRITTEN_SHOPS:]}


def write_profiles(kb_dir: Path) -> list[Path]:
    """Write every generated profile under ``kb_dir`` (LF line endings on every OS)."""
    written = []
    for relative, content in generated_profiles().items():
        path = kb_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def out_of_date(kb_dir: Path) -> list[str]:
    """Generated profiles that are missing or differ from what the catalogue produces."""
    problems = []
    for relative, content in generated_profiles().items():
        path = kb_dir / relative
        if not path.is_file():
            problems.append(f"missing: {relative}")
        elif path.read_bytes().decode("utf-8") != content:
            problems.append(f"out of date: {relative}")
    return problems
