"""Every shop has one profile document, and it agrees with the shop catalogue."""

from pathlib import Path

import pytest
import yaml

from app.seed.catalog import CUSTOMER_MIX, SHOPS, ShopSpec
from app.seed.profiles import (
    HAND_WRITTEN_SHOPS,
    generated_profiles,
    out_of_date,
    profile_path,
    render_profile,
)

KB_DIR = Path(__file__).resolve().parent.parent / "knowledge_base"


def _profile_files() -> dict[str, tuple[dict, str]]:
    """shop_id -> (front-matter, body) for every shop-profile document."""
    found: dict[str, tuple[dict, str]] = {}
    for path in sorted((KB_DIR / "shops").glob("*.md")):
        _, meta, body = path.read_text(encoding="utf-8").split("---\n", 2)
        data = yaml.safe_load(meta)
        assert data["shop_id"] not in found, f"two profiles for {data['shop_id']}"
        found[data["shop_id"]] = (data, body)
    return found


def test_every_shop_has_exactly_one_profile() -> None:
    assert set(_profile_files()) == {shop.id for shop in SHOPS}


@pytest.mark.parametrize("spec", SHOPS, ids=lambda s: s.id)
def test_profile_agrees_with_the_catalogue(spec: ShopSpec) -> None:
    meta, body = _profile_files()[spec.id]

    assert meta["document_id"] == f"SHOP-PROFILE-{spec.id[-3:]}"
    assert meta["category"] == "shop_profile" and meta["status"] == "current"
    assert meta["title"].endswith(spec.name)
    assert spec.name in body and spec.owner_name in body and spec.staff_name in body
    assert spec.locality in body
    assert "\n## Basics" in body and "\n## House rules" in body


@pytest.mark.parametrize("spec", SHOPS[HAND_WRITTEN_SHOPS:], ids=lambda s: s.id)
def test_generated_profile_quotes_the_credit_limit_the_data_uses(spec: ShopSpec) -> None:
    _, body = _profile_files()[spec.id]
    top_limit = max(limit for _, _, share, limit in CUSTOMER_MIX[spec.shop_type] if share > 0)
    assert f"Rs {top_limit:,}" in body


def test_generated_profiles_are_up_to_date() -> None:
    # If this fails, run: python -m scripts.generate_shop_profiles
    assert out_of_date(KB_DIR) == []


def test_generated_profiles_cover_exactly_the_shops_without_a_hand_written_one() -> None:
    assert len(generated_profiles()) == len(SHOPS) - HAND_WRITTEN_SHOPS == 45
    for spec in SHOPS[:HAND_WRITTEN_SHOPS]:
        assert profile_path(spec) not in generated_profiles()


def test_rendering_is_deterministic_and_shops_differ() -> None:
    assert render_profile(SHOPS[10]) == render_profile(SHOPS[10])
    hours = {
        line
        for spec in SHOPS[HAND_WRITTEN_SHOPS:]
        for line in render_profile(spec).splitlines()
        if line.startswith("- Hours:")
    }
    assert len(hours) > 10  # not 45 copies of the same opening hours


def test_awkward_names_cannot_break_the_front_matter() -> None:
    spec = ShopSpec(
        "SHOP-099", "Raj: Tools & Paints", "hardware", "A B", "C D", "Old Town",
        SHOPS[0].opened_on, "HW", 10, (1,),
    )  # fmt: skip
    _, meta, _ = render_profile(spec).split("---\n", 2)
    assert yaml.safe_load(meta)["title"] == "Shop profile — Raj: Tools & Paints"
