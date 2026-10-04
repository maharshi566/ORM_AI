"""ChromaDB vector store.

ChromaDB runs inside the backend process (``PersistentClient``) and keeps its files
in ``CHROMA_PERSIST_DIR`` (``backend/data/chroma``, git-ignored). There is no
server to start.

Chunks are stored in one collection per group, so each kind of document can be
searched, counted and rebuilt on its own, and outside documents stay apart:

==========  ========================================
Collection  Categories
==========  ========================================
policies    policy
sops        sop
faqs        faq
reference   supplier_terms, shop_profile
external    supplier_flyer (untrusted outside material)
==========  ========================================

Collection names include the embedding model (``orm_policies__text-embedding-3-small``),
because vectors from different models cannot be compared.

The functions the project spec asks for are ``add_documents``,
``search_documents``, ``metadata_filter``, ``update_document`` and
``delete_document``.

ChromaDB's embedded client is synchronous: it blocks while it reads and writes
files. ``VectorStore`` is that blocking layer. Async code (the API, the agents,
ingestion) uses ``AsyncVectorStore`` instead, which runs every call in a worker
thread so the event loop stays free to serve other requests.
"""

import asyncio
import hashlib
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import chromadb
from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings as ChromaSettings

from app.rag.chunking import Chunk
from app.rag.open_files import raise_open_file_limit

COLLECTION_GROUPS: dict[str, frozenset[str]] = {
    "policies": frozenset({"policy"}),
    "sops": frozenset({"sop"}),
    "faqs": frozenset({"faq"}),
    "reference": frozenset({"supplier_terms", "shop_profile"}),
    "external": frozenset({"supplier_flyer"}),
}
_GROUP_OF = {category: group for group, cats in COLLECTION_GROUPS.items() for category in cats}


def group_for_category(category: str) -> str:
    return _GROUP_OF.get(category, "reference")


# What ChromaDB says when an index is missing from its in-memory cache and not saved yet.
LOST_INDEX = "Nothing found on disk"


class VectorStoreError(RuntimeError):
    """ChromaDB failed (files locked or damaged, disk full, bad query)."""


@dataclass(frozen=True)
class SearchFilters:
    """Which chunks a search may return. The defaults suit an answer for a shop today."""

    categories: frozenset[str] | None = None  # None = every category
    current_only: bool = True  # False also returns superseded versions
    as_of: date | None = None  # only documents already in effect on this date
    shop_id: str | None = None  # shop-specific documents of this shop; None = shared only
    include_untrusted: bool = False  # outside material such as supplier flyers
    document_ids: frozenset[str] | None = None

    def groups(self) -> list[str]:
        if self.categories:
            groups = {group_for_category(category) for category in self.categories}
        else:
            groups = set(COLLECTION_GROUPS)
            if not self.include_untrusted:
                groups.discard("external")
        return sorted(groups)


def metadata_filter(filters: SearchFilters) -> dict[str, Any] | None:
    """The filters as a ChromaDB ``where`` clause."""
    clauses: list[dict[str, Any]] = []
    if filters.categories:
        clauses.append({"category": {"$in": sorted(filters.categories)}})
    if filters.current_only:
        clauses.append({"status": "current"})
    if filters.as_of:
        clauses.append({"effective_date_int": {"$lte": int(filters.as_of.strftime("%Y%m%d"))}})
    clauses.append(
        {"shop_id": {"$in": ["", filters.shop_id]}} if filters.shop_id else {"shop_id": ""}
    )
    if not filters.include_untrusted:
        clauses.append({"trust": "trusted"})
    if filters.document_ids:
        clauses.append({"document_id": {"$in": sorted(filters.document_ids)}})
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def matches_filter(filters: SearchFilters, metadata: dict[str, Any]) -> bool:
    """The same rules as ``metadata_filter``, checked in Python (used by keyword search)."""
    if filters.categories and metadata.get("category") not in filters.categories:
        return False
    if filters.current_only and metadata.get("status") != "current":
        return False
    if filters.as_of and int(metadata.get("effective_date_int", 0)) > int(
        filters.as_of.strftime("%Y%m%d")
    ):
        return False
    if metadata.get("shop_id", "") not in {"", filters.shop_id or ""}:
        return False
    if not filters.include_untrusted and metadata.get("trust") != "trusted":
        return False
    return not (filters.document_ids and metadata.get("document_id") not in filters.document_ids)


@dataclass
class StoredChunk:
    id: str
    text: str  # the chunk's content
    metadata: dict[str, Any]
    similarity: float | None = None  # cosine similarity to the query (vector search only)


def _slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-._") or "model"


class VectorStore:
    def __init__(
        self, persist_dir: Path | str, embedding_model: str, *, client: ClientAPI | None = None
    ) -> None:
        self.persist_dir = Path(persist_dir)
        self.embedding_model = embedding_model
        if client is None:
            # ChromaDB sizes its in-memory index cache from the open-file limit and reads it
            # once, when the client is created. Windows and macOS report small limits, which
            # makes it forget indexes at random. See app/rag/open_files.py.
            raise_open_file_limit()
            client = chromadb.PersistentClient(
                path=str(self.persist_dir), settings=ChromaSettings(anonymized_telemetry=False)
            )
        self._client = client
        self._collections: dict[str, Collection] = {}

    # ------------------------------------------------------------ helpers

    @contextmanager
    def _guard(self, action: str) -> Iterator[None]:
        try:
            yield
        except VectorStoreError:
            raise
        except Exception as exc:  # ChromaDB raises many types, including sqlite errors
            reason = " ".join(str(exc).split())[:300].rstrip(".")
            message = f"ChromaDB could not {action} ({type(exc).__name__})"
            if reason:
                message += f": {reason}"
            if LOST_INDEX in reason:
                message += (
                    ". ChromaDB dropped a search index from memory; restart the backend and "
                    'it is rebuilt (docs/rag.md, "Nothing found on disk")'
                )
            raise VectorStoreError(message + ".") from exc

    def collection_name(self, group: str) -> str:
        return f"orm_{group}__{_slug(self.embedding_model)}"

    def _collection(self, group: str, *, create: bool) -> Collection | None:
        if group in self._collections:
            return self._collections[group]
        name = self.collection_name(group)
        if create:
            collection = self._client.get_or_create_collection(
                name,
                embedding_function=None,  # we always pass vectors; nothing is downloaded
                metadata={"embedding_model": self.embedding_model, "group": group},
                configuration={"hnsw": {"space": "cosine"}},
            )
        else:
            if name not in {c.name for c in self._client.list_collections()}:
                return None
            collection = self._client.get_collection(name, embedding_function=None)
        self._collections[group] = collection
        return collection

    def _each(self, groups: list[str] | None = None) -> Iterator[tuple[str, Collection]]:
        for group in groups or list(COLLECTION_GROUPS):
            collection = self._collection(group, create=False)
            if collection is not None:
                yield group, collection

    # ------------------------------------------------------------- writes

    def add_documents(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int:
        """Store chunks with their vectors. Upsert, so adding the same chunk twice is safe."""
        if len(chunks) != len(embeddings):
            raise ValueError("one embedding per chunk is required")
        by_group: dict[str, list[int]] = {}
        for index, chunk in enumerate(chunks):
            by_group.setdefault(group_for_category(chunk.metadata.category), []).append(index)
        with self._guard("store chunks"):
            for group, indexes in by_group.items():
                collection = self._collection(group, create=True)
                if collection is None:  # cannot happen with create=True; keeps type checkers happy
                    raise VectorStoreError(f"collection {group} could not be created")
                collection.upsert(
                    ids=[chunks[i].id for i in indexes],
                    embeddings=[embeddings[i] for i in indexes],
                    documents=[chunks[i].content for i in indexes],
                    metadatas=[chunks[i].store_metadata() for i in indexes],  # type: ignore[misc]
                )
        return len(chunks)

    def delete_ids(self, ids: set[str]) -> int:
        """Delete chunks by ID, wherever they are. Returns how many were deleted."""
        deleted = 0
        with self._guard("delete chunks"):
            for _, collection in self._each():
                present = collection.get(ids=sorted(ids), include=[])["ids"] if ids else []
                if present:
                    collection.delete(ids=present)
                    deleted += len(present)
        return deleted

    def delete_document(self, document_key: str) -> int:
        """Delete every chunk of one document version, e.g. ``POL-CREDIT-001@v1``."""
        deleted = 0
        with self._guard("delete a document"):
            for _, collection in self._each():
                present = collection.get(where={"document_key": document_key}, include=[])["ids"]
                if present:
                    collection.delete(ids=present)
                    deleted += len(present)
        return deleted

    def update_document(
        self, document_key: str, chunks: list[Chunk], embeddings: list[list[float]]
    ) -> int:
        """Replace one document version's chunks with new ones."""
        self.delete_document(document_key)
        return self.add_documents(chunks, embeddings)

    def reset(self) -> None:
        """Delete this embedding model's collections (``ingest --rebuild``)."""
        with self._guard("delete collections"):
            for group, _ in list(self._each()):
                self._client.delete_collection(self.collection_name(group))
        self._collections.clear()

    # -------------------------------------------------------------- reads

    def search_documents(
        self, query_embedding: list[float], *, k: int = 5, filters: SearchFilters | None = None
    ) -> list[StoredChunk]:
        """The ``k`` chunks most similar to the query vector that pass the filters."""
        filters = filters or SearchFilters()
        where = metadata_filter(filters)
        results: list[StoredChunk] = []
        with self._guard("search"):
            for _, collection in self._each(filters.groups()):
                size = collection.count()
                if size == 0:
                    continue
                found = collection.query(
                    query_embeddings=[query_embedding],
                    n_results=min(k, size),
                    where=where,
                    include=["documents", "metadatas", "distances"],
                )
                for chunk_id, text, meta, distance in zip(
                    found["ids"][0],
                    found["documents"][0],  # type: ignore[index]
                    found["metadatas"][0],  # type: ignore[index]
                    found["distances"][0],  # type: ignore[index]
                    strict=True,
                ):
                    results.append(StoredChunk(chunk_id, text, dict(meta), 1.0 - distance))
        results.sort(key=lambda chunk: chunk.similarity or 0.0, reverse=True)
        return results[:k]

    def get_chunks(self, filters: SearchFilters | None = None) -> list[StoredChunk]:
        """Every stored chunk (optionally filtered), for keyword search."""
        where = metadata_filter(filters) if filters else None
        chunks: list[StoredChunk] = []
        with self._guard("read chunks"):
            groups = filters.groups() if filters else None
            for _, collection in self._each(groups):
                found = collection.get(where=where, include=["documents", "metadatas"])
                for chunk_id, text, meta in zip(
                    found["ids"],
                    found["documents"],  # type: ignore[arg-type]
                    found["metadatas"],  # type: ignore[arg-type]
                    strict=True,
                ):
                    chunks.append(StoredChunk(chunk_id, text, dict(meta)))
        return chunks

    def ids_by_group(self) -> dict[str, set[str]]:
        with self._guard("list chunk IDs"):
            return {
                group: set(collection.get(include=[])["ids"]) for group, collection in self._each()
            }

    def ids(self) -> set[str]:
        return {chunk_id for ids in self.ids_by_group().values() for chunk_id in ids}

    def count(self) -> dict[str, int]:
        with self._guard("count chunks"):
            return {group: collection.count() for group, collection in self._each()}

    def fingerprint(self) -> str:
        """Changes whenever chunks are added or removed (used to refresh caches)."""
        return hashlib.sha256("\n".join(sorted(self.ids())).encode()).hexdigest()


class AsyncVectorStore:
    """The async face of ``VectorStore``: the same methods, each run in a worker thread.

    Use this from ``async def`` code. Calling the plain ``VectorStore`` there would
    freeze the whole API for as long as ChromaDB takes to answer. Calls run one at a
    time, because the embedded ChromaDB client keeps cached collection handles and
    writes its files through a single SQLite database anyway.
    """

    def __init__(self, store: VectorStore) -> None:
        self.sync = store  # the blocking store; only touch it from a worker thread
        self._lock = asyncio.Lock()

    @property
    def embedding_model(self) -> str:
        return self.sync.embedding_model

    @property
    def persist_dir(self) -> Path:
        return self.sync.persist_dir

    def collection_name(self, group: str) -> str:
        return self.sync.collection_name(group)

    async def _run[T](self, call: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        async with self._lock:
            return await asyncio.to_thread(call, *args, **kwargs)

    async def add_documents(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int:
        return await self._run(self.sync.add_documents, chunks, embeddings)

    async def delete_ids(self, ids: set[str]) -> int:
        return await self._run(self.sync.delete_ids, ids)

    async def delete_document(self, document_key: str) -> int:
        return await self._run(self.sync.delete_document, document_key)

    async def update_document(
        self, document_key: str, chunks: list[Chunk], embeddings: list[list[float]]
    ) -> int:
        return await self._run(self.sync.update_document, document_key, chunks, embeddings)

    async def reset(self) -> None:
        await self._run(self.sync.reset)

    async def search_documents(
        self, query_embedding: list[float], *, k: int = 5, filters: SearchFilters | None = None
    ) -> list[StoredChunk]:
        return await self._run(self.sync.search_documents, query_embedding, k=k, filters=filters)

    async def get_chunks(self, filters: SearchFilters | None = None) -> list[StoredChunk]:
        return await self._run(self.sync.get_chunks, filters)

    async def ids_by_group(self) -> dict[str, set[str]]:
        return await self._run(self.sync.ids_by_group)

    async def ids(self) -> set[str]:
        return await self._run(self.sync.ids)

    async def count(self) -> dict[str, int]:
        return await self._run(self.sync.count)

    async def fingerprint(self) -> str:
        return await self._run(self.sync.fingerprint)


def as_async(store: "VectorStore | AsyncVectorStore") -> AsyncVectorStore:
    """The async wrapper for ``store`` (already-wrapped stores are returned unchanged)."""
    return store if isinstance(store, AsyncVectorStore) else AsyncVectorStore(store)
