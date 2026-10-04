"""Build the retrieval pipeline from settings."""

from app.config.paths import backend_path
from app.config.settings import Settings
from app.rag.embeddings import build_embedder
from app.rag.reranker import build_reranker
from app.rag.retriever import KnowledgeRetriever
from app.rag.vector_store import VectorStore


def build_retriever(
    settings: Settings, *, embedding_model: str | None = None
) -> KnowledgeRetriever:
    """A retriever over the ingested knowledge base.

    Raises EmbeddingConfigError when the embedding model needs an API key that is
    missing. Nothing is downloaded or embedded here.
    """
    embedder = build_embedder(settings, embedding_model)
    store = VectorStore(backend_path(settings.chroma_persist_dir), embedder.model)
    return KnowledgeRetriever(
        store,
        embedder,
        reranker=build_reranker(settings),
        min_similarity=settings.retrieval_min_similarity,
    )
