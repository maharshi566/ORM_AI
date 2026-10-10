"""GET /api/knowledge/search: try the knowledge search from the browser.

Open http://localhost:8000/docs, choose this endpoint, click "Try it out". It runs
the same search the Knowledge agent's ``search_knowledge`` tool runs, without an
agent or an LLM answer: you see exactly which passages and citations come back.

Shared documents are searched for everyone. A shop's own documents (its profile, its
uploads) only for that shop: a logged-in user always searches their own shop.
"""

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.config.settings import Settings
from app.core.auth import CurrentUser, check_shop
from app.core.exceptions import DependencyUnavailableError
from app.rag.retriever import KnowledgeBaseEmptyError
from app.rag.vector_store import SearchFilters, VectorStoreError
from app.services.knowledge_service import shared_retriever
from app.tools.base import business_now
from app.tools.knowledge_tools import Category, SearchKnowledgeOutput, search_output

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get(
    "/search",
    response_model=SearchKnowledgeOutput,
    summary="Search the shop's policies and documents",
)
async def search(
    request: Request,
    principal: CurrentUser,
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
    if principal is not None and principal.role != "admin":
        if shop_id is not None:
            check_shop(principal, shop_id)
        shop_id = principal.shop_id
    filters = SearchFilters(
        categories=frozenset(category) if category else None,
        current_only=not include_superseded,
        as_of=business_now(settings.business_date).date(),
        shop_id=shop_id,
        include_untrusted=include_untrusted,
    )
    try:
        retriever = await shared_retriever(request.app)
        result = await retriever.search(q, k=k, filters=filters)
    except (KnowledgeBaseEmptyError, VectorStoreError) as exc:
        raise DependencyUnavailableError(str(exc)) from exc
    return search_output(result)
