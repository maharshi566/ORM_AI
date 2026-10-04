"""GET /api/knowledge/search: try the knowledge search from the browser.

Open http://localhost:8000/docs, choose this endpoint, click "Try it out". It runs
the same search the Knowledge agent's ``search_knowledge`` tool runs, without an
agent or an LLM answer: you see exactly which passages and citations come back.
"""

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.config.settings import Settings
from app.core.exceptions import DependencyUnavailableError
from app.rag.embeddings import EmbeddingConfigError
from app.rag.factory import build_retriever
from app.rag.retriever import KnowledgeBaseEmptyError, KnowledgeRetriever
from app.rag.vector_store import SearchFilters, VectorStoreError
from app.tools.base import business_now
from app.tools.knowledge_tools import Category, SearchKnowledgeOutput, search_output

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def _retriever(request: Request) -> KnowledgeRetriever:
    """Built on first use and kept on the app, so ChromaDB is opened once."""
    retriever = getattr(request.app.state, "knowledge_retriever", None)
    if retriever is None:
        settings: Settings = request.app.state.settings
        try:
            retriever = build_retriever(settings)
        except EmbeddingConfigError as exc:
            raise DependencyUnavailableError(str(exc)) from exc
        request.app.state.knowledge_retriever = retriever
    return retriever


@router.get(
    "/search",
    response_model=SearchKnowledgeOutput,
    summary="Search the shop's policies and documents",
)
async def search(
    request: Request,
    q: Annotated[str, Query(min_length=3, max_length=500, description="The question")],
    k: Annotated[int, Query(ge=1, le=10, description="How many passages")] = 5,
    category: Annotated[list[Category] | None, Query(description="Limit to these")] = None,
    shop_id: Annotated[
        str | None, Query(description="Include this shop's own profile, e.g. SHOP-004")
    ] = None,
    include_superseded: bool = False,
    include_untrusted: bool = False,
) -> SearchKnowledgeOutput:
    settings: Settings = request.app.state.settings
    filters = SearchFilters(
        categories=frozenset(category) if category else None,
        current_only=not include_superseded,
        as_of=business_now(settings.business_date).date(),
        shop_id=shop_id,
        include_untrusted=include_untrusted,
    )
    try:
        result = await _retriever(request).search(q, k=k, filters=filters)
    except (KnowledgeBaseEmptyError, VectorStoreError) as exc:
        raise DependencyUnavailableError(str(exc)) from exc
    return search_output(result)
