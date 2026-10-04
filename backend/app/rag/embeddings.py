"""Embeddings: turning text into vectors that can be compared by meaning.

One interface, ``Embedder``, with two implementations chosen by ``EMBEDDING_MODEL``:

* ``OpenAIEmbedder`` (any ``text-embedding-*`` model, default
  ``text-embedding-3-small``): real semantic search. Needs ``OPENAI_API_KEY``.
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
    """OpenAI embeddings API. The SDK already retries 429 and 5xx responses with
    exponential backoff (``max_retries``); what still fails becomes EmbeddingError."""

    # text-embedding-3 models score unrelated text around 0.0-0.2 and related
    # text around 0.3-0.7 (cosine similarity).
    min_similarity = 0.25

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        batch_size: int = 100,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.model = model
        self.batch_size = batch_size
        self._client = client or AsyncOpenAI(
            api_key=api_key, timeout=timeout, max_retries=max_retries
        )

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            # The API rejects empty strings.
            batch = [
                text if text.strip() else " " for text in texts[start : start + self.batch_size]
            ]
            try:
                response = await self._client.embeddings.create(model=self.model, input=batch)
            except openai.AuthenticationError as exc:
                raise EmbeddingError(
                    "OpenAI rejected the API key. Check OPENAI_API_KEY in .env.", retryable=False
                ) from exc
            except openai.RateLimitError as exc:
                raise EmbeddingError(
                    "OpenAI rate limit or quota reached. Wait a minute, and check the "
                    "account's billing and limits if it keeps happening.",
                    retryable=True,
                ) from exc
            except (openai.APITimeoutError, openai.APIConnectionError) as exc:
                raise EmbeddingError(
                    "Could not reach the OpenAI API (timeout or network error).", retryable=True
                ) from exc
            except openai.APIStatusError as exc:
                raise EmbeddingError(
                    f"OpenAI embeddings failed with HTTP {exc.status_code}.",
                    retryable=exc.status_code >= 500,
                ) from exc
            except openai.OpenAIError as exc:
                raise EmbeddingError(
                    f"OpenAI embeddings failed ({type(exc).__name__}).", retryable=False
                ) from exc
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
    key = settings.openai_api_key.get_secret_value().strip() if settings.openai_api_key else ""
    if not key:
        raise EmbeddingConfigError(
            f"EMBEDDING_MODEL is {name}, which needs OPENAI_API_KEY in .env. To try "
            "without a key, set EMBEDDING_MODEL=hash or pass --embedding-model hash."
        )
    return OpenAIEmbedder(
        name, key, timeout=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries
    )
