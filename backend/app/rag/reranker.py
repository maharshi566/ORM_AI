"""Rerankers: put the retrieved candidates in their final order.

Chosen with ``RERANKER`` in ``.env``:

* ``heuristic`` (default): fast and free. Starts from the fused search score and
  adds small, explainable bonuses: the question's words appear in the section
  heading or the document title. Superseded versions and untrusted outside
  documents are pushed down, so the current rule is cited first.
* ``llm``: asks the fast LLM (``LLM_MODEL_FAST``) to score each candidate 0-3 and
  blends that with the heuristic score. More accurate on subtle questions, costs a
  little per search, and falls back to the heuristic on any error or timeout.
* ``none``: keeps the fused search order.

A cross-encoder model would also work here, but it means installing PyTorch
(about 2 GB), so it is left out.
"""

import asyncio
import json
from typing import TYPE_CHECKING, Protocol

from openai import AsyncOpenAI

from app.config.settings import Settings
from app.core.logging import get_logger
from app.prompts.rerank_prompt import RERANK_PROMPT
from app.rag.text import search_terms

if TYPE_CHECKING:
    from app.rag.retriever import RetrievedChunk

logger = get_logger(__name__)


class Reranker(Protocol):
    name: str

    async def rerank(
        self, query: str, chunks: list["RetrievedChunk"], k: int
    ) -> list["RetrievedChunk"]: ...


class NoReranker:
    name = "none"

    async def rerank(
        self, query: str, chunks: list["RetrievedChunk"], k: int
    ) -> list["RetrievedChunk"]:
        return sorted(chunks, key=lambda chunk: chunk.fused_score, reverse=True)


class HeuristicReranker:
    name = "heuristic"
    SECTION_BONUS = 0.15
    TITLE_BONUS = 0.10
    SUPERSEDED_PENALTY = 0.25
    UNTRUSTED_PENALTY = 0.10

    async def rerank(
        self, query: str, chunks: list["RetrievedChunk"], k: int
    ) -> list["RetrievedChunk"]:
        terms = set(search_terms(query))
        for chunk in chunks:
            score = chunk.fused_score
            if terms:
                score += (
                    self.SECTION_BONUS * len(terms & set(search_terms(chunk.section))) / len(terms)
                )
                score += self.TITLE_BONUS * len(terms & set(search_terms(chunk.title))) / len(terms)
            if chunk.status != "current":
                score -= self.SUPERSEDED_PENALTY
            if chunk.trust != "trusted":
                score -= self.UNTRUSTED_PENALTY
            chunk.score = score
        return sorted(chunks, key=lambda chunk: chunk.score, reverse=True)


class LLMReranker:
    name = "llm"
    LLM_WEIGHT = 0.7

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        *,
        fallback: Reranker | None = None,
        timeout_seconds: float = 10.0,
        max_candidates: int = 12,
        text_chars: int = 700,
    ) -> None:
        self._client = client
        self.model = model
        self.fallback = fallback or HeuristicReranker()
        self.timeout_seconds = timeout_seconds
        self.max_candidates = max_candidates
        self.text_chars = text_chars

    async def rerank(
        self, query: str, chunks: list["RetrievedChunk"], k: int
    ) -> list["RetrievedChunk"]:
        ordered = await self.fallback.rerank(query, chunks, k)
        if len(ordered) <= 1:
            return ordered
        candidates, rest = ordered[: self.max_candidates], ordered[self.max_candidates :]
        payload = {
            "question": query,
            "passages": [
                {"id": i, "source": chunk.citation, "text": chunk.text[: self.text_chars]}
                for i, chunk in enumerate(candidates)
            ],
        }
        try:
            response = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": RERANK_PROMPT.render()},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    response_format={"type": "json_object"},
                ),
                self.timeout_seconds,
            )
            data = json.loads(response.choices[0].message.content or "{}")
            scores = {int(item["id"]): float(item["score"]) for item in data["scores"]}
        except Exception as exc:  # any failure: keep the heuristic order
            logger.warning("llm_rerank_failed", error_type=type(exc).__name__)
            return ordered
        for i, chunk in enumerate(candidates):
            llm_score = min(3.0, max(0.0, scores.get(i, 0.0))) / 3
            chunk.score = self.LLM_WEIGHT * llm_score + (1 - self.LLM_WEIGHT) * chunk.score
        candidates.sort(key=lambda chunk: chunk.score, reverse=True)
        return candidates + rest


def build_reranker(settings: Settings) -> Reranker:
    if settings.reranker == "none":
        return NoReranker()
    if settings.reranker == "llm":
        key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
        if key and settings.llm_model_fast:
            client = AsyncOpenAI(api_key=key, timeout=settings.llm_timeout_seconds, max_retries=1)
            return LLMReranker(client, settings.llm_model_fast)
        logger.warning(
            "llm_reranker_unavailable",
            reason="RERANKER=llm needs OPENAI_API_KEY and LLM_MODEL_FAST; using heuristic",
        )
    return HeuristicReranker()
