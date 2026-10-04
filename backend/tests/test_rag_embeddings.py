"""Embedders: the offline hash embedder, the OpenAI adapter (with a fake client) and
the factory that picks one from settings."""

import math
from types import SimpleNamespace

import httpx2
import openai
import pytest

from app.config.settings import Settings
from app.rag.embeddings import (
    EmbeddingConfigError,
    EmbeddingError,
    HashEmbedder,
    OpenAIEmbedder,
    build_embedder,
)
from app.services.llm_service import LLMEndpoint


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_hash_vectors_are_deterministic_normalised_and_lexical() -> None:
    embedder = HashEmbedder(256)
    credit, credit_again, cement = await embedder.embed_documents(
        ["Household credit limit Rs 3,000", "household credit limits", "cement truck delivery"]
    )

    assert credit == (await embedder.embed_documents(["Household credit limit Rs 3,000"]))[0]
    assert len(credit) == 256
    assert math.isclose(sum(v * v for v in credit), 1.0)
    assert cosine(credit, credit_again) > cosine(credit, cement)


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_factory_picks_the_embedder_from_settings() -> None:
    assert build_embedder(_settings(embedding_model="hash")).model == "hash-512"
    assert build_embedder(_settings(), "hash-128").model == "hash-128"
    openai_embedder = build_embedder(
        _settings(embedding_model="text-embedding-3-small", openai_api_key="sk-test")
    )
    assert isinstance(openai_embedder, OpenAIEmbedder)
    assert openai_embedder.model == "text-embedding-3-small"


def test_openai_model_without_a_key_explains_what_to_do() -> None:
    with pytest.raises(EmbeddingConfigError, match="OPENAI_API_KEY"):
        build_embedder(_settings(embedding_model="text-embedding-3-small", openai_api_key=None))


class FakeEmbeddings:
    """Mimics client.embeddings.create; returns items in reverse order on purpose."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.options: dict[str, object] = {}
        self.error = error

    async def create(
        self,
        *,
        model: str,
        input: list[str],
        **options: object,  # noqa: A002
    ) -> SimpleNamespace:
        self.calls.append(list(input))
        self.options = options
        if self.error:
            raise self.error
        items = [
            SimpleNamespace(index=i, embedding=[float(len(text)), float(i)])
            for i, text in enumerate(input)
        ]
        return SimpleNamespace(data=list(reversed(items)))


def openai_embedder(fake: FakeEmbeddings, batch_size: int = 2) -> OpenAIEmbedder:
    client = SimpleNamespace(embeddings=fake)
    endpoint = LLMEndpoint("embeddings", None, "sk-test", "OPENAI_API_KEY")
    return OpenAIEmbedder(
        "text-embedding-3-small",
        endpoint,
        batch_size=batch_size,
        client=client,  # type: ignore[arg-type]
    )


async def test_openai_embedder_batches_and_keeps_order() -> None:
    fake = FakeEmbeddings()
    vectors = await openai_embedder(fake).embed_documents(["a", "bb", "", "dddd", "eeeee"])

    assert [len(call) for call in fake.calls] == [2, 2, 1]
    assert fake.calls[1] == [" ", "dddd"]  # empty strings are not sent as-is
    assert [v[0] for v in vectors] == [1.0, 2.0, 1.0, 4.0, 5.0]


_REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/embeddings")


def _status_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    return cls("x", response=httpx2.Response(status, request=_REQUEST), body=None)


@pytest.mark.parametrize(
    ("error", "retryable", "message"),
    [
        (_status_error(openai.AuthenticationError, 401), False, "API key"),
        (_status_error(openai.RateLimitError, 429), True, "rate limit"),
        (_status_error(openai.InternalServerError, 503), True, "HTTP 503"),
        (openai.APITimeoutError(request=_REQUEST), True, "Could not reach"),
    ],
)
async def test_openai_errors_become_clear_embedding_errors(
    error: Exception, retryable: bool, message: str
) -> None:
    with pytest.raises(EmbeddingError, match=message) as excinfo:
        await openai_embedder(FakeEmbeddings(error)).embed_query("credit limit")

    assert excinfo.value.retryable is retryable
