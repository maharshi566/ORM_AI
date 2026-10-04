"""Loading and cleaning knowledge-base files: Markdown, text and PDF."""

from datetime import date
from pathlib import Path

import pytest

from app.rag.loaders import (
    LoaderError,
    clean_pdf_pages,
    clean_text,
    load_directory,
    load_file,
)
from tests.conftest import KB_DIR

FRONT_MATTER = """---
document_id: POL-TEST-001
title: Test policy
source: internal
category: policy
version: 3
effective_date: 2026-05-01
status: current
---
"""


def make_pdf(pages: list[list[str]]) -> bytes:
    """A minimal PDF: one Helvetica text line per string, one list per page."""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(len(pages)))}]"
            f" /Count {len(pages)} >>"
        ).encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, lines in enumerate(pages):
        ops = ["BT", "/F1 12 Tf", "72 750 Td"]
        for j, line in enumerate(lines):
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops += (["0 -16 Td"] if j else []) + [f"({escaped}) Tj"]
        stream = "\n".join([*ops, "ET"]).encode("latin-1")
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>"
            ).encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def test_the_real_knowledge_base_loads() -> None:
    docs = load_directory(KB_DIR)

    assert len(docs) == 33
    assert all(doc.format == "markdown" for doc in docs)
    assert not any(doc.path.endswith("README.md") for doc in docs)
    keys = [doc.metadata.key for doc in docs]
    assert "POL-CREDIT-001@v1" in keys and "POL-CREDIT-001@v2" in keys
    assert len(set(keys)) == len(keys)


def test_metadata_is_parsed_and_outside_content_is_untrusted() -> None:
    docs = {doc.metadata.key: doc.metadata for doc in load_directory(KB_DIR)}

    credit = docs["POL-CREDIT-001@v2"]
    assert credit.effective_date == date(2026, 4, 1)
    assert credit.is_current and credit.trust == "trusted" and credit.shop_id is None
    assert not docs["POL-CREDIT-001@v1"].is_current
    assert docs["SHOP-PROFILE-004@v1"].shop_id == "SHOP-004"
    assert docs["EXT-FLYER-001@v1"].trust == "untrusted"


def test_cleaning_keeps_structure_and_removes_noise() -> None:
    raw = "## Heading\r\n\r\n\r\n\r\nText​ with   \r\n<!-- note -->| a | b |\r\n"

    assert clean_text(raw) == "## Heading\n\nText with\n| a | b |"


def test_markdown_file_loads_with_windows_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "policies" / "test.md"
    path.parent.mkdir()
    path.write_bytes(
        (FRONT_MATTER + "# Title\n\n## 1. Rule\n\nBody text.\n").encode().replace(b"\n", b"\r\n")
    )

    doc = load_file(path, tmp_path)

    assert doc.metadata.key == "POL-TEST-001@v3"
    assert doc.path == "policies/test.md"
    assert doc.text == "# Title\n\n## 1. Rule\n\nBody text."
    assert len(doc.content_hash) == 64


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("# No front-matter\n", "missing front-matter"),
        ("---\ntitle: x\n---\nBody\n", "missing front-matter fields"),
        (FRONT_MATTER.replace("category: policy", "category: memo") + "Body\n", "unknown category"),
        (FRONT_MATTER.replace("2026-05-01", "May 2026") + "Body\n", "not a YYYY-MM-DD date"),
        (FRONT_MATTER, "has no text"),
    ],
)
def test_bad_files_get_clear_errors(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "bad.md"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(LoaderError, match=message):
        load_file(path, tmp_path)


def test_directory_errors_list_every_bad_file_and_duplicate_keys(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(FRONT_MATTER + "Body\n", encoding="utf-8")
    (tmp_path / "b.md").write_text(FRONT_MATTER + "Other body\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("no metadata\n", encoding="utf-8")

    with pytest.raises(LoaderError) as excinfo:
        load_directory(tmp_path)

    assert "c.md: missing front-matter" in str(excinfo.value)
    assert "POL-TEST-001@v3 is already used by a.md" in str(excinfo.value)


def test_pdf_pages_lose_headers_footers_and_page_numbers() -> None:
    pages = [
        "ACME PRICE LIST\nRice costs Rs 1,450.\nPage 1 of 3",
        "ACME PRICE LIST\nSugar costs Rs 2,100. Order manage-\nment is weekly.\n2",
        "ACME PRICE LIST\nDelivery in 2 days.\nPage 3 of 3",
    ]

    assert clean_pdf_pages(pages) == [
        "Rice costs Rs 1,450.",
        "Sugar costs Rs 2,100. Order management is weekly.",
        "Delivery in 2 days.",
    ]


def test_pdf_with_side_file_metadata(tmp_path: Path) -> None:
    folder = tmp_path / "suppliers"
    folder.mkdir()
    pdf = folder / "acme-prices.pdf"
    pdf.write_bytes(
        make_pdf(
            [
                ["ACME PRICE LIST", "Rice 25 kg costs Rs 1,450.", "Page 1 of 3"],
                ["ACME PRICE LIST", "Sugar 50 kg costs Rs 2,100.", "Page 2 of 3"],
                ["ACME PRICE LIST", "Delivery within 2 days.", "Page 3 of 3"],
            ]
        )
    )
    (folder / "acme-prices.meta.yaml").write_text(
        "document_id: SUP-ACME-PRICES\ntitle: ACME price list\nsource: supplier\n"
        "category: supplier_terms\nversion: 1\neffective_date: 2026-09-01\n",
        encoding="utf-8",
    )

    doc = load_file(pdf, tmp_path)

    assert doc.format == "pdf"
    assert doc.metadata.document_id == "SUP-ACME-PRICES"
    assert [(page.number, page.text) for page in doc.pages] == [
        (1, "Rice 25 kg costs Rs 1,450."),
        (2, "Sugar 50 kg costs Rs 2,100."),
        (3, "Delivery within 2 days."),
    ]
    assert doc.warnings == []


def test_pdf_without_side_file_gets_derived_metadata_and_a_warning(tmp_path: Path) -> None:
    folder = tmp_path / "external"
    folder.mkdir()
    pdf = folder / "festival flyer.pdf"
    pdf.write_bytes(make_pdf([["Big festival offer on biscuits."]]))

    doc = load_file(pdf, tmp_path)

    assert doc.metadata.document_id == "FESTIVAL-FLYER"
    assert doc.metadata.category == "supplier_flyer"
    assert doc.metadata.trust == "untrusted"
    assert doc.warnings and "guessed" in doc.warnings[0]


def test_text_files_load(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text(FRONT_MATTER + "Plain text rules.\n", encoding="utf-8")

    doc = load_file(path, tmp_path)

    assert doc.format == "text" and doc.text == "Plain text rules."
