"""Heading-aware chunking, size limits, overlap and stable content-hash IDs."""

from dataclasses import replace
from datetime import date

import pytest

from app.rag.chunking import chunk_document, chunk_documents, split_sections, split_text
from app.rag.loaders import DocumentMetadata, LoadedDocument, Page, load_directory
from app.rag.text import estimate_tokens
from tests.conftest import KB_DIR

META = DocumentMetadata(
    document_id="POL-TEST-001",
    title="Test policy",
    source="internal",
    category="policy",
    version=2,
    effective_date=date(2026, 4, 1),
)


def doc(text: str, *, meta: DocumentMetadata = META, fmt: str = "markdown") -> LoadedDocument:
    return LoadedDocument(meta, "policies/test.md", fmt, [Page(text)], "0" * 64)  # type: ignore[arg-type]


def test_sections_follow_headings() -> None:
    text = (
        "# Test policy\n\nIntro line.\n\n## 1. Limits\n\nRs 3,000.\n\n"
        "### Households\n\nSpecial rule.\n\n```\n## not a heading\n```\n\n## 2. Terms\n\n30 days."
    )

    sections = split_sections(doc(text))

    assert [s.name for s in sections] == [
        "Introduction",
        "1. Limits",
        "1. Limits › Households",
        "2. Terms",
    ]
    assert "## not a heading" in sections[2].text  # headings inside code blocks are text


def test_chunks_carry_metadata_and_citations() -> None:
    chunks = chunk_document(doc("# T\n\n## 2. Credit limits\n\nHousehold Rs 3,000."))

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.citation == "[POL-TEST-001 v2 §2. Credit limits]"
    assert chunk.embedding_text.startswith("Test policy\n2. Credit limits\n\n")
    meta = chunk.store_metadata()
    assert meta["document_key"] == "POL-TEST-001@v2"
    assert meta["effective_date_int"] == 20260401
    assert meta["shop_id"] == "" and meta["page"] == 0  # ChromaDB rejects None
    assert all(isinstance(v, str | int) for v in meta.values())


def test_long_sections_are_split_within_the_limit_with_overlap() -> None:
    paragraphs = [f"Paragraph {i}. " + "word " * 120 for i in range(8)]
    pieces = split_text("\n\n".join(paragraphs), max_tokens=200, overlap_tokens=30)

    assert len(pieces) > 1
    assert all(estimate_tokens(piece) <= 200 for piece in pieces)
    assert all(piece.startswith("… ") for piece in pieces[1:])  # overlap from the previous piece


def test_big_tables_split_between_rows_and_keep_the_header() -> None:
    table = "| Item | Price |\n| --- | --- |\n" + "\n".join(
        f"| item {i} | " + "x" * 60 + " |" for i in range(40)
    )

    pieces = split_text(table, max_tokens=200, overlap_tokens=0)

    assert len(pieces) > 1
    assert all(piece.startswith("| Item | Price |\n| --- | --- |") for piece in pieces)
    assert all(estimate_tokens(piece) <= 200 for piece in pieces)


def test_pdf_and_text_documents_have_page_and_text_sections() -> None:
    pdf = LoadedDocument(META, "x.pdf", "pdf", [Page("One", 1), Page("Two", 2)], "0" * 64)
    txt = doc("Plain text.", fmt="text")

    assert [(c.section, c.page) for c in chunk_document(pdf)] == [("Page 1", 1), ("Page 2", 2)]
    assert [c.section for c in chunk_document(txt)] == ["Text"]


def test_ids_are_stable_and_change_with_content_or_metadata() -> None:
    text = "## 1. Rule\n\nSame text."
    first = chunk_document(doc(text))[0].id

    assert chunk_document(doc(text))[0].id == first
    assert chunk_document(doc(text + " Edited."))[0].id != first
    superseded = replace(META, status="superseded")
    assert chunk_document(doc(text, meta=superseded))[0].id != first


def test_identical_paragraphs_in_one_section_get_different_ids() -> None:
    pieces = "\n\n".join(["Repeat " * 120] * 3)
    chunks = chunk_document(doc(f"## 1. Rule\n\n{pieces}"), max_tokens=200, overlap_tokens=0)

    assert len({c.id for c in chunks}) == len(chunks)


def test_invalid_sizes_are_rejected() -> None:
    with pytest.raises(ValueError):
        chunk_document(doc("## A\n\nB"), max_tokens=10)
    with pytest.raises(ValueError):
        chunk_document(doc("## A\n\nB"), max_tokens=100, overlap_tokens=60)


def test_real_knowledge_base_chunks() -> None:
    chunks = chunk_documents(load_directory(KB_DIR))

    assert 100 <= len(chunks) <= 200
    assert len({c.id for c in chunks}) == len(chunks)
    assert max(c.token_count for c in chunks) <= 600
    limits = [c for c in chunks if c.citation == "[POL-CREDIT-001 v2 §2. Credit limits]"]
    assert len(limits) == 1 and "Rs 3,000" in limits[0].content
