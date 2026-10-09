"""The knowledge retriever the whole app shares (the search route and the agents).

Built on first use and kept on ``app.state``, so ChromaDB is opened once per process.
Opening ChromaDB reads files, so it runs in a worker thread. The lock makes several
simultaneous first requests share one retriever instead of each building their own.
"""

import asyncio

from fastapi import FastAPI

from app.config.settings import Settings
from app.core.exceptions import DependencyUnavailableError
from app.rag.embeddings import EmbeddingConfigError
from app.rag.factory import build_retriever
from app.rag.retriever import KnowledgeRetriever


async def shared_retriever(app: FastAPI) -> KnowledgeRetriever:
    state = app.state
    if getattr(state, "knowledge_retriever", None) is None:
        async with state.knowledge_lock:
            if getattr(state, "knowledge_retriever", None) is None:
                settings: Settings = state.settings
                try:
                    state.knowledge_retriever = await asyncio.to_thread(build_retriever, settings)
                except EmbeddingConfigError as exc:
                    raise DependencyUnavailableError(str(exc)) from exc
    return state.knowledge_retriever
