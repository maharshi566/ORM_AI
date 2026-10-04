"""Checks every knowledge-base document's front-matter and structure."""

from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest
import yaml

KB_DIR = Path(__file__).resolve().parent.parent / "knowledge_base"
REQUIRED = {"document_id", "title", "source", "category", "version", "effective_date", "status"}
CATEGORIES = {"policy", "sop", "supplier_terms", "faq", "shop_profile", "supplier_flyer"}
SOURCES = {"internal", "supplier", "external"}


def _documents() -> list[Path]:
    return sorted(p for p in KB_DIR.rglob("*.md") if p.name != "README.md")


def _front_matter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name}: missing front-matter"
    _, meta, body = text.split("---\n", 2)
    return yaml.safe_load(meta), body


def test_there_are_enough_documents() -> None:
    # The spec asks for 20 to 50 knowledge documents. Shop profiles are counted
    # separately: there is one per shop (see test_shop_profiles.py).
    categories = [_front_matter(path)[0]["category"] for path in _documents()]
    assert 20 <= len([c for c in categories if c != "shop_profile"]) <= 50
    assert categories.count("shop_profile") == 50


@pytest.mark.parametrize("path", _documents(), ids=lambda p: p.name)
def test_front_matter_is_complete(path: Path) -> None:
    meta, body = _front_matter(path)

    assert meta.keys() >= REQUIRED, f"missing: {REQUIRED - meta.keys()}"
    assert meta["category"] in CATEGORIES
    assert meta["source"] in SOURCES
    assert meta["status"] in {"current", "superseded"}
    assert isinstance(meta["version"], int)
    assert isinstance(meta["effective_date"], date)
    assert "\n## " in body, "use ## headings so the chunker can split sections"


def test_each_document_has_exactly_one_current_version() -> None:
    versions: dict[str, list[dict]] = defaultdict(list)
    for path in _documents():
        meta, _ = _front_matter(path)
        versions[meta["document_id"]].append(meta)

    for document_id, metas in versions.items():
        current = [m for m in metas if m["status"] == "current"]
        assert len(current) == 1, document_id
        assert current[0]["version"] == max(m["version"] for m in metas), document_id


def test_credit_policy_has_a_superseded_version_for_conflict_tests() -> None:
    metas = [_front_matter(p)[0] for p in _documents()]
    credit = sorted((m for m in metas if m["document_id"] == "POL-CREDIT-001"),
                    key=lambda m: m["version"])  # fmt: skip
    assert [m["status"] for m in credit] == ["superseded", "current"]


def test_outside_content_is_marked_untrusted() -> None:
    for path in _documents():
        meta, _ = _front_matter(path)
        if meta["source"] == "external":
            assert meta.get("trust") == "untrusted", path.name
