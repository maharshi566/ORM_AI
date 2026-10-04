"""Ingestion: idempotent, picks up edits and deletions, keeps the database in sync."""

import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Document, DocumentChunk
from app.rag.embeddings import EmbeddingError, HashEmbedder
from app.rag.ingestion import ingest_knowledge_base
from app.rag.vector_store import VectorStore
from tests.conftest import KB_DIR


@pytest.fixture
def kb_copy(tmp_path: Path) -> Path:
    target = tmp_path / "kb"
    shutil.copytree(KB_DIR, target)
    return target


@pytest.fixture
def store(tmp_path: Path) -> VectorStore:
    return VectorStore(tmp_path / "chroma", HashEmbedder().model)


async def test_dry_run_counts_without_storing(kb_copy: Path) -> None:
    report = await ingest_knowledge_base(kb_dir=kb_copy, store=None, embedder=None, dry_run=True)

    assert report.documents == 33 and report.chunks > 100
    assert report.added == 0 and report.dry_run


async def test_running_twice_creates_no_duplicates(kb_copy: Path, store: VectorStore) -> None:
    first = await ingest_knowledge_base(kb_dir=kb_copy, store=store, embedder=HashEmbedder())
    second = await ingest_knowledge_base(kb_dir=kb_copy, store=store, embedder=HashEmbedder())

    assert first.added == first.chunks and first.embedded_tokens > 0
    assert second.added == 0 and second.removed == 0 and second.unchanged == second.chunks
    assert sum(store.count().values()) == first.chunks


def _faq_stock_chunks(store: VectorStore) -> int:
    return sum(1 for c in store.get_chunks() if c.metadata["document_id"] == "FAQ-003")


async def test_edits_and_deletions_are_picked_up(kb_copy: Path, store: VectorStore) -> None:
    await ingest_knowledge_base(kb_dir=kb_copy, store=store, embedder=HashEmbedder())
    deleted_chunks = _faq_stock_chunks(store)
    pricing = kb_copy / "policies" / "pricing-policy.md"
    pricing.write_text(
        pricing.read_text(encoding="utf-8").replace("Minimum margin: 8%", "Minimum margin: 9%"),
        encoding="utf-8",
    )
    (kb_copy / "faqs" / "faq-stock.md").unlink()

    report = await ingest_knowledge_base(kb_dir=kb_copy, store=store, embedder=HashEmbedder())

    assert deleted_chunks == 5
    assert report.added == 1  # only the edited section is embedded again
    assert report.removed == 1 + deleted_chunks  # the old section + the deleted FAQ
    assert sum(store.count().values()) == report.chunks
    texts = [c.text for c in store.get_chunks() if c.metadata["document_id"] == "POL-PRICING-001"]
    assert any("9%" in text for text in texts) and not any("margin: 8%" in t for t in texts)


async def test_rebuild_embeds_everything_again(kb_copy: Path, store: VectorStore) -> None:
    await ingest_knowledge_base(kb_dir=kb_copy, store=store, embedder=HashEmbedder())

    report = await ingest_knowledge_base(
        kb_dir=kb_copy, store=store, embedder=HashEmbedder(), rebuild=True
    )

    assert report.added == report.chunks


class FailingEmbedder(HashEmbedder):
    def __init__(self, fail_on_call: int) -> None:
        super().__init__()
        self.calls = 0
        self.fail_on_call = fail_on_call

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise EmbeddingError("rate limited", retryable=True)
        return await super().embed_documents(texts)


async def test_an_interrupted_run_keeps_its_progress(kb_copy: Path, store: VectorStore) -> None:
    with pytest.raises(EmbeddingError):
        await ingest_knowledge_base(
            kb_dir=kb_copy, store=store, embedder=FailingEmbedder(2), batch_size=50
        )
    assert sum(store.count().values()) == 50  # the first batch was stored

    report = await ingest_knowledge_base(
        kb_dir=kb_copy, store=store, embedder=HashEmbedder(), batch_size=50
    )
    assert report.added == report.chunks - 50


async def _count(factory: async_sessionmaker[AsyncSession], model: type) -> int:
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def test_database_tables_match_the_knowledge_base(
    kb_copy: Path, store: VectorStore, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    first = await ingest_knowledge_base(
        kb_dir=kb_copy, store=store, embedder=HashEmbedder(), session_factory=session_factory
    )
    again = await ingest_knowledge_base(
        kb_dir=kb_copy, store=store, embedder=HashEmbedder(), session_factory=session_factory
    )

    assert first.database.startswith("updated") and again.database.startswith("updated")
    assert await _count(session_factory, Document) == 33
    assert await _count(session_factory, DocumentChunk) == first.chunks
    async with session_factory() as session:
        v1 = await session.get(Document, "POL-CREDIT-001@v1")
        dairy = await session.get(Document, "SHOP-PROFILE-004@v1")
    assert v1 is not None and v1.is_current is False
    assert dairy is not None and dairy.shop_id == "SHOP-004"

    deleted_chunks = _faq_stock_chunks(store)
    (kb_copy / "faqs" / "faq-stock.md").unlink()
    await ingest_knowledge_base(
        kb_dir=kb_copy, store=store, embedder=HashEmbedder(), session_factory=session_factory
    )
    assert await _count(session_factory, Document) == 32
    assert await _count(session_factory, DocumentChunk) == first.chunks - deleted_chunks


async def test_database_failure_is_reported_not_raised(kb_copy: Path, store: VectorStore) -> None:
    def broken_factory() -> AsyncSession:
        raise ConnectionRefusedError("database is down")

    report = await ingest_knowledge_base(
        kb_dir=kb_copy,
        store=store,
        embedder=HashEmbedder(),
        session_factory=broken_factory,  # type: ignore[arg-type]
    )

    assert report.database_failed and "database is down" in report.database
    assert sum(store.count().values()) == report.chunks  # the vectors were still stored
