"""GET /api/knowledge/search."""

from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.main import create_app
from app.rag.vector_store import VectorStore


def _client(store: VectorStore, **overrides: object) -> TestClient:
    values: dict[str, object] = {
        "app_env": "test",
        "embedding_model": "hash",
        "chroma_persist_dir": str(store.persist_dir),
        "business_date": "2026-09-30",
        "database_url": "postgresql+asyncpg://test:test@localhost:5432/orm_ai_test",
        "redis_url": "redis://localhost:6379/15",
    }
    values.update(overrides)
    return TestClient(create_app(Settings(_env_file=None, **values)))  # type: ignore[arg-type]


def test_search_returns_cited_passages(kb_store: VectorStore) -> None:
    with _client(kb_store) as client:
        response = client.get(
            "/api/knowledge/search", params={"q": "household credit limit", "k": 3}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True and len(body["passages"]) <= 3
    assert "[POL-CREDIT-001 v2 §2. Credit limits]" in [p["citation"] for p in body["passages"]]


def test_shop_profile_needs_its_shop_id(kb_store: VectorStore) -> None:
    params = {"q": "opening hours owner staff", "category": "shop_profile"}
    with _client(kb_store) as client:
        shared = client.get("/api/knowledge/search", params=params).json()
        dairy = client.get("/api/knowledge/search", params={**params, "shop_id": "SHOP-004"}).json()

    assert shared["found"] is False
    assert {p["document_id"] for p in dairy["passages"]} == {"SHOP-PROFILE-004"}


def test_invalid_query_is_a_422(kb_store: VectorStore) -> None:
    with _client(kb_store) as client:
        response = client.get("/api/knowledge/search", params={"q": "ab"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_missing_api_key_is_a_503_with_instructions(kb_store: VectorStore) -> None:
    with _client(kb_store, embedding_model="text-embedding-3-small", openai_api_key=None) as client:
        response = client.get("/api/knowledge/search", params={"q": "credit limit"})

    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["error"]["message"]
