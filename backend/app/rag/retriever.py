"""Hybrid retrieval: meaning search plus keyword search, then filtering and reranking.

How one search works (``KnowledgeRetriever.search``):

1. **Meaning search.** Embed the question and ask ChromaDB for the most similar
   chunks that pass the filters (category, current version, effective date, shop,
   trust; see ``SearchFilters``).
2. **Keyword search.** Score every chunk that passes the same filters with BM25,
   the classic ranking formula of search engines: rare words that appear often in
   a chunk count most. It catches exact terms such as "UPI reference" or
   "SUP-004" that meaning search can blur.
3. **Fuse.** Combine the two rankings with Reciprocal Rank Fusion: each chunk scores
   1/(60 + rank) in each list it appears in. Robust, and needs no tuning.
4. **Relevance gate.** Keep a chunk only if it is clearly similar in meaning
   (``embedder.min_similarity``) or contains at least half of the question's
   words. An unrelated question therefore returns nothing, and the agent says
   "not found" instead of guessing.
5. **Rerank** the survivors (``reranker.py``) and return the top ``k``.

If the embedding API is down, steps 1 and 3 are skipped and the result says
``keyword_only=True``: degraded, but still grounded in real documents.
"""

import asyncio
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.rag.chunking import format_citation
from app.rag.context import looks_like_injection
from app.rag.embeddings import Embedder, EmbeddingError
from app.rag.reranker import HeuristicReranker, Reranker
from app.rag.text import search_terms
from app.rag.vector_store import (
    AsyncVectorStore,
    SearchFilters,
    StoredChunk,
    VectorStore,
    as_async,
    matches_filter,
)

logger = get_logger(__name__)

RRF_K = 60
MIN_KEYWORD_COVERAGE = 0.5


class KnowledgeBaseEmptyError(RuntimeError):
    """Nothing has been ingested for this embedding model yet."""


@dataclass
class RetrievedChunk:
    id: str
    citation: str
    document_id: str
    document_key: str
    title: str
    version: int
    status: str
    category: str
    section: str
    page: int | None
    effective_date: str
    source: str
    trust: str
    shop_id: str | None
    text: str
    score: float = 0.0  # final score after reranking; higher is better
    fused_score: float = 0.0  # 0-1, from rank fusion
    similarity: float | None = None  # cosine similarity (meaning search)
    keyword_score: float = 0.0  # BM25
    keyword_coverage: float = 0.0  # share of the question's words found in the chunk
    vector_rank: int | None = None
    keyword_rank: int | None = None
    suspicious: bool = False  # contains instruction-like text (possible prompt injection)

    @classmethod
    def from_stored(cls, stored: StoredChunk) -> "RetrievedChunk":
        meta = stored.metadata
        version = int(meta.get("version", 1))
        section = str(meta.get("section", ""))
        return cls(
            id=stored.id,
            citation=format_citation(str(meta.get("document_id", "")), version, section),
            document_id=str(meta.get("document_id", "")),
            document_key=str(meta.get("document_key", "")),
            title=str(meta.get("title", "")),
            version=version,
            status=str(meta.get("status", "current")),
            category=str(meta.get("category", "")),
            section=section,
            page=int(meta["page"]) if meta.get("page") else None,
            effective_date=str(meta.get("effective_date", "")),
            source=str(meta.get("source", "")),
            trust=str(meta.get("trust", "trusted")),
            shop_id=str(meta["shop_id"]) if meta.get("shop_id") else None,
            text=stored.text,
        )


@dataclass
class RetrievalResult:
    query: str
    chunks: list[RetrievedChunk]
    keyword_only: bool = False
    warnings: list[str] = field(default_factory=list)
    best_similarity: float | None = None  # best meaning-search score, before the gate

    @property
    def found(self) -> bool:
        return bool(self.chunks)


class KeywordIndex:
    """An in-memory BM25 index over every stored chunk.

    The knowledge base is small (about 130 chunks), so the index is rebuilt in a few
    milliseconds whenever the stored chunks change. A large knowledge base would use
    a search engine or PostgreSQL full-text search instead.
    """

    def __init__(self, chunks: list[StoredChunk], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.chunks = chunks
        self.k1, self.b = k1, b
        terms = [
            search_terms(f"{c.metadata.get('title', '')} {c.metadata.get('section', '')} {c.text}")
            for c in chunks
        ]
        self._tf = [Counter(t) for t in terms]
        self._lengths = [len(t) for t in terms]
        self._avg_length = (sum(self._lengths) / len(terms)) if terms else 0.0
        df = Counter(term for counts in self._tf for term in counts)
        n = len(chunks)
        self._idf = {term: math.log(1 + (n - d + 0.5) / (d + 0.5)) for term, d in df.items()}

    def search(
        self, query: str, *, k: int, filters: SearchFilters
    ) -> list[tuple[StoredChunk, float, float]]:
        """Top ``k`` chunks as (chunk, BM25 score, coverage of the query's words)."""
        query_terms = set(search_terms(query))
        if not query_terms:
            return []
        hits = []
        for chunk, tf, length in zip(self.chunks, self._tf, self._lengths, strict=True):
            if not matches_filter(filters, chunk.metadata):
                continue
            matched = [term for term in query_terms if term in tf]
            if not matched:
                continue
            score = 0.0
            for term in matched:
                freq = tf[term]
                norm = 1 - self.b + self.b * length / (self._avg_length or 1)
                score += self._idf[term] * freq * (self.k1 + 1) / (freq + self.k1 * norm)
            hits.append((chunk, score, len(matched) / len(query_terms)))
        hits.sort(key=lambda hit: hit[1], reverse=True)
        return hits[:k]


class KnowledgeRetriever:
    def __init__(
        self,
        store: VectorStore | AsyncVectorStore,
        embedder: Embedder,
        *,
        reranker: Reranker | None = None,
        candidate_pool: int = 20,
        min_similarity: float | None = None,
    ) -> None:
        self.store = as_async(store)  # ChromaDB blocks, so it is always called in a thread
        self.embedder = embedder
        self.reranker = reranker or HeuristicReranker()
        self.candidate_pool = candidate_pool
        self.min_similarity = (
            min_similarity if min_similarity is not None else embedder.min_similarity
        )
        self._index: KeywordIndex | None = None
        self._index_fingerprint: str | None = None
        self._index_lock = asyncio.Lock()

    async def _keyword_index(self) -> KeywordIndex:
        async with self._index_lock:
            fingerprint = await self.store.fingerprint()
            if self._index is None or fingerprint != self._index_fingerprint:
                chunks = await self.store.get_chunks()
                if not chunks:
                    raise KnowledgeBaseEmptyError(
                        "The knowledge base is empty for embedding model "
                        f"{self.store.embedding_model}. Run: python -m scripts.ingest"
                    )
                index = await asyncio.to_thread(KeywordIndex, chunks)  # CPU work: off the loop
                self._index, self._index_fingerprint = index, fingerprint
            return self._index

    async def search(
        self, query: str, *, k: int = 5, filters: SearchFilters | None = None
    ) -> RetrievalResult:
        query = query.strip()
        if not query:
            raise ValueError("query is empty")
        filters = filters or SearchFilters()
        pool = max(self.candidate_pool, k * 4)
        result = RetrievalResult(query=query, chunks=[])

        index = await self._keyword_index()
        keyword_hits = index.search(query, k=pool, filters=filters)

        vector_hits: list[StoredChunk] = []
        try:
            query_vector = await self.embedder.embed_query(query)
            vector_hits = await self.store.search_documents(query_vector, k=pool, filters=filters)
            if vector_hits:
                result.best_similarity = vector_hits[0].similarity
        except EmbeddingError as exc:
            result.keyword_only = True
            result.warnings.append(
                f"Meaning search is unavailable ({exc}). These results come from keyword "
                "search only."
            )
            logger.warning("embedding_failed_keyword_only", error=str(exc))

        candidates = self._fuse(vector_hits, keyword_hits)
        relevant = [
            chunk
            for chunk in candidates
            if (chunk.similarity or 0.0) >= self.min_similarity
            or chunk.keyword_coverage >= MIN_KEYWORD_COVERAGE
        ]
        ranked = await self.reranker.rerank(query, relevant, k)
        for chunk in ranked:
            chunk.suspicious = looks_like_injection(chunk.text)
        result.chunks = ranked[:k]
        logger.info(
            "knowledge_search",
            query_length=len(query),
            vector_hits=len(vector_hits),
            keyword_hits=len(keyword_hits),
            relevant=len(relevant),
            returned=len(result.chunks),
            keyword_only=result.keyword_only,
            reranker=self.reranker.name,
        )
        return result

    @staticmethod
    def _fuse(
        vector_hits: list[StoredChunk], keyword_hits: list[tuple[StoredChunk, float, float]]
    ) -> list[RetrievedChunk]:
        merged: dict[str, RetrievedChunk] = {}
        rrf: dict[str, float] = Counter()
        for rank, stored in enumerate(vector_hits, start=1):
            chunk = merged.setdefault(stored.id, RetrievedChunk.from_stored(stored))
            chunk.similarity, chunk.vector_rank = stored.similarity, rank
            rrf[stored.id] += 1 / (RRF_K + rank)
        for rank, (stored, score, coverage) in enumerate(keyword_hits, start=1):
            chunk = merged.setdefault(stored.id, RetrievedChunk.from_stored(stored))
            chunk.keyword_score, chunk.keyword_coverage, chunk.keyword_rank = score, coverage, rank
            rrf[stored.id] += 1 / (RRF_K + rank)
        best_possible = 2 / (RRF_K + 1)  # first in both lists
        for chunk_id, chunk in merged.items():
            chunk.fused_score = chunk.score = rrf[chunk_id] / best_possible
        return sorted(merged.values(), key=lambda chunk: chunk.fused_score, reverse=True)


def describe(result: RetrievalResult) -> list[dict[str, Any]]:
    """A compact view of a result for logs and debugging."""
    return [
        {
            "citation": chunk.citation,
            "score": round(chunk.score, 3),
            "similarity": None if chunk.similarity is None else round(chunk.similarity, 3),
            "keyword_rank": chunk.keyword_rank,
            "vector_rank": chunk.vector_rank,
        }
        for chunk in result.chunks
    ]
