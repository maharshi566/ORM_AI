"""Ingestion: knowledge-base files -> chunks -> vectors in ChromaDB (+ database rows).

Run with ``python -m scripts.ingest``. Ingestion is the only place embeddings are
created; the API never re-embeds on startup.

It is safe to run again at any time:

* A chunk's ID is a hash of its text and metadata, so unchanged chunks are found
  by ID and skipped: no API call, no cost, no duplicates.
* Chunks that no longer exist (a file was edited or deleted) are removed.
* An interrupted run keeps what it stored; the next run continues from there.

The ``documents`` and ``document_chunks`` tables get the same picture, so the
admin pages and audits (Phase 6-7) can list what the assistant can cite without
opening ChromaDB.
"""

import asyncio
import dataclasses
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Document, DocumentChunk, Shop
from app.rag.chunking import DEFAULT_MAX_TOKENS, DEFAULT_OVERLAP_TOKENS, Chunk, chunk_documents
from app.rag.embeddings import Embedder
from app.rag.loaders import LoadedDocument, load_directory, load_uploads
from app.rag.vector_store import (
    COLLECTION_GROUPS,
    AsyncVectorStore,
    VectorStore,
    as_async,
    group_for_category,
)


@dataclass
class GroupStats:
    chunks: int = 0
    added: int = 0
    unchanged: int = 0
    removed: int = 0


@dataclass
class IngestReport:
    embedding_model: str
    documents: int = 0
    chunks: int = 0
    embedded_tokens: int = 0  # roughly what the embedding API was billed for
    by_group: dict[str, GroupStats] = field(default_factory=dict)
    database: str = "skipped"
    dry_run: bool = False
    seconds: float = 0.0
    skipped_uploads: list[str] = field(default_factory=list)  # uploads that could not be used

    def group(self, name: str) -> GroupStats:
        return self.by_group.setdefault(name, GroupStats())

    @property
    def added(self) -> int:
        return sum(g.added for g in self.by_group.values())

    @property
    def unchanged(self) -> int:
        return sum(g.unchanged for g in self.by_group.values())

    @property
    def removed(self) -> int:
        return sum(g.removed for g in self.by_group.values())

    @property
    def database_failed(self) -> bool:
        return self.database.startswith("failed")


def load_all(kb_dir: Path, upload_dir: Path | None) -> tuple[list[LoadedDocument], list[str]]:
    """The knowledge base plus usable uploads (paths start "uploads/"), and the uploads
    that were skipped. A problem in the knowledge base itself still stops everything."""
    docs = load_directory(kb_dir)
    if upload_dir is None:
        return docs, []
    uploads, skipped = load_uploads(upload_dir)
    taken = {doc.metadata.key for doc in docs}
    for doc in uploads:
        if doc.metadata.key in taken:  # cannot happen with UPL- IDs; never replace a policy
            skipped.append(f"uploads/{doc.path}: {doc.metadata.key} is a knowledge-base ID")
            continue
        docs.append(dataclasses.replace(doc, path=f"uploads/{doc.path}"))
    return docs, skipped


async def ingest_knowledge_base(
    *,
    kb_dir: Path,
    upload_dir: Path | None = None,
    store: VectorStore | AsyncVectorStore | None,
    embedder: Embedder | None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    rebuild: bool = False,
    dry_run: bool = False,
    batch_size: int = 64,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> IngestReport:
    started = time.perf_counter()
    # Reading files (and PDFs) and chunking are blocking work, so they run in a thread.
    docs, skipped = await asyncio.to_thread(load_all, kb_dir, upload_dir)
    chunks = await asyncio.to_thread(
        chunk_documents, docs, max_tokens=max_tokens, overlap_tokens=overlap_tokens
    )
    report = IngestReport(
        embedding_model=embedder.model if embedder else "-",
        documents=len(docs),
        chunks=len(chunks),
        dry_run=dry_run,
        skipped_uploads=skipped,
    )
    for name in COLLECTION_GROUPS:
        report.group(name)
    for chunk in chunks:
        report.group(group_for_category(chunk.metadata.category)).chunks += 1
    if dry_run:
        report.seconds = time.perf_counter() - started
        return report
    if store is None or embedder is None:
        raise ValueError("store and embedder are required unless dry_run is set")
    store = as_async(store)

    if rebuild:
        await store.reset()
    existing = await store.ids_by_group()
    existing_ids = {chunk_id for ids in existing.values() for chunk_id in ids}
    wanted_ids = {chunk.id for chunk in chunks}

    new = [chunk for chunk in chunks if chunk.id not in existing_ids]
    for start in range(0, len(new), batch_size):
        batch = new[start : start + batch_size]
        vectors = await embedder.embed_documents([chunk.embedding_text for chunk in batch])
        await store.add_documents(batch, vectors)
        report.embedded_tokens += sum(chunk.token_count for chunk in batch)
        for chunk in batch:
            report.group(group_for_category(chunk.metadata.category)).added += 1

    stale = existing_ids - wanted_ids
    await store.delete_ids(stale)
    for group, ids in existing.items():
        report.group(group).removed = len(ids & stale)
    for stats in report.by_group.values():
        stats.unchanged = stats.chunks - stats.added

    if session_factory is not None:
        try:
            await sync_database(session_factory, docs, chunks)
            report.database = f"updated ({len(docs)} documents, {len(chunks)} chunks)"
        except Exception as exc:  # the vectors are stored; report and let the caller decide
            report.database = f"failed ({type(exc).__name__}: {str(exc).splitlines()[0][:200]})"
    report.seconds = time.perf_counter() - started
    return report


async def sync_database(
    session_factory: async_sessionmaker[AsyncSession],
    docs: list[LoadedDocument],
    chunks: list[Chunk],
) -> None:
    """Make the documents and document_chunks tables match the knowledge base."""
    async with session_factory() as session:
        shop_ids = set((await session.scalars(select(Shop.id))).all())
        wanted_docs = {doc.metadata.key: doc for doc in docs}
        wanted_chunks = {chunk.id for chunk in chunks}
        existing_docs = set((await session.scalars(select(Document.id))).all())

        await session.execute(
            delete(DocumentChunk).where(
                DocumentChunk.id.not_in(wanted_chunks)
                | DocumentChunk.document_pk.not_in(list(wanted_docs))
            )
        )
        stale_docs = existing_docs - set(wanted_docs)
        if stale_docs:
            await session.execute(delete(Document).where(Document.id.in_(stale_docs)))

        for key, doc in wanted_docs.items():
            meta = doc.metadata
            await session.merge(
                Document(
                    id=key,
                    document_id=meta.document_id,
                    version=meta.version,
                    title=meta.title[:200],
                    source=meta.source,
                    category=meta.category,
                    # Only link shops that exist (the database may not be seeded yet).
                    shop_id=meta.shop_id if meta.shop_id in shop_ids else None,
                    effective_date=meta.effective_date,
                    is_current=meta.is_current,
                    path=doc.path[:300],
                    content_hash=doc.content_hash,
                )
            )
        await session.flush()
        for chunk in chunks:
            await session.merge(
                DocumentChunk(
                    id=chunk.id,
                    document_pk=chunk.document_key,
                    chunk_index=chunk.chunk_index,
                    section=chunk.section[:200],
                    page=chunk.page,
                    content=chunk.content,
                    token_count=chunk.token_count,
                )
            )
        await session.commit()
