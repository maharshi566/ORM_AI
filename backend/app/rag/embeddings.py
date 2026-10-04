"""Embeddings: turning text into vectors that can be compared by meaning.

One interface, ``Embedder``, with two implementations chosen by ``EMBEDDING_MODEL``:

* ``OpenAIEmbedder`` (any ``text-embedding-*`` model, default
  ``text-embedding-3-small``): real semantic search. Needs ``OPENAI_API_KEY``, or a
  gateway that speaks the OpenAI API (``LLM_BASE_URL`` / ``EMBEDDING_BASE_URL``, for
  example OmniRoute; see docs/omniroute.md).
* ``HashEmbedder`` (``EMBEDDING_MODEL=hash``): offline and free. It maps words and
  word pairs to vector positions with a hash ("feature hashing"), so it matches
  shared words, not meanings. Used by the tests and CI, and handy for trying the
  pipeline before you have an API key.

Each model's vectors live in their own ChromaDB collections, because vectors from
different models cannot be compared with each other.
"""

import hashlib
import math
from typing import Protocol

import openai
from openai import AsyncOpenAI

from app.config.settings import Settings
from app.rag.text import search_terms
from app.services.llm_service import (
    LLMConfigError,
    LLMEndpoint,
    explain_error,
    make_client,
    resolve_endpoint,
)

DEFAULT_HASH_DIMENSIONS = 512


class EmbeddingError(RuntimeError):
    """The embedding call failed. ``retryable`` is True for temporary failures."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class EmbeddingConfigError(ValueError):
    """The embedding model cannot be used as configured (for example, no API key)."""


class Embedder(Protocol):
    model: str  # stored with the vectors; changing it means re-ingesting
    # Results less similar than this are treated as "not related" by the retriever.
    min_similarity: float

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


# ------------------------------------------------------------------ offline


class HashEmbedder:
    """Deterministic bag-of-words vectors. Not semantic; see the module docstring."""

    # Measured on the test set: off-topic questions score up to ~0.15 through
    # hash collisions, while correct answers score 0.2-0.55.
    min_similarity = 0.2

    def __init__(self, dimensions: int = DEFAULT_HASH_DIMENSIONS) -> None:
        if dimensions < 16:
            raise ValueError("dimensions must be at least 16")
        self.dimensions = dimensions
        self.model = f"hash-{dimensions}"

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        terms = search_terms(text)
        features = [(term, 1.0) for term in terms]
        features += [(f"{a}_{b}", 0.5) for a, b in zip(terms, terms[1:], strict=False)]
        for feature, weight in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


# ------------------------------------------------------------------- OpenAI


class OpenAIEmbedder:
    """Embeddings from OpenAI or any server that speaks the same API (a gateway).

    The SDK already retries 429 and 5xx responses with exponential backoff
    (``max_retries``); what still fails becomes EmbeddingError, with a message that
    names where the call went (``endpoint.where``) and what to try.
    """

    # text-embedding-3 models score unrelated text around 0.0-0.2 and related
    # text around 0.3-0.7 (cosine similarity). Other models behave differently:
    # set RETRIEVAL_MIN_SIMILARITY after running scripts.eval_retrieval.
    min_similarity = 0.25

    def __init__(
        self,
        model: str,
        endpoint: LLMEndpoint,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        batch_size: int = 100,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.model = model
        self.endpoint = endpoint
        self.batch_size = batch_size
        self._client = client or make_client(endpoint, timeout=timeout, max_retries=max_retries)

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            # The API rejects empty strings.
            batch = [
                text if text.strip() else " " for text in texts[start : start + self.batch_size]
            ]
            try:
                # "float" is spelled out because the SDK would otherwise ask for base64
                # (smaller), which not every gateway understands.
                response = await self._client.embeddings.create(
                    model=self.model, input=batch, encoding_format="float"
                )
            except openai.OpenAIError as exc:
                failure = explain_error(exc, self.endpoint, doing="embeddings", model=self.model)
                raise EmbeddingError(failure.message, retryable=failure.retryable) from exc
            vectors.extend(item.embedding for item in sorted(response.data, key=lambda d: d.index))
        return vectors

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text]))[0]


# ------------------------------------------------------------------ factory


def build_embedder(settings: Settings, model: str | None = None) -> Embedder:
    """The embedder for ``model`` (default: ``settings.embedding_model``)."""
    name = (model or settings.embedding_model).strip()
    if name == "hash" or name.startswith("hash-"):
        dimensions = int(name.split("-", 1)[1]) if "-" in name else DEFAULT_HASH_DIMENSIONS
        return HashEmbedder(dimensions)
    try:
        endpoint = resolve_endpoint(settings, "embeddings")
    except LLMConfigError as exc:
        raise EmbeddingConfigError(str(exc)) from exc
    if endpoint is None:
        raise EmbeddingConfigError(
            f"EMBEDDING_MODEL is {name}, which needs OPENAI_API_KEY in .env (or LLM_BASE_URL "
            "for a gateway such as OmniRoute). To try without a key, set EMBEDDING_MODEL=hash "
            "or pass --embedding-model hash."
        )
    return OpenAIEmbedder(
        name, endpoint, timeout=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries
    )
