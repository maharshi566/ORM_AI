"""The retrieval test set is valid, and the offline pipeline clears the gate.

The real gate (hit@5 >= 0.8) is measured with OpenAI embeddings by
``python -m scripts.eval_retrieval``. This test runs the same evaluation with the
offline hash embedder, so CI catches retrieval regressions without an API key.
"""

from datetime import date

from app.config.paths import BACKEND_DIR
from app.rag.chunking import chunk_documents
from app.rag.embeddings import HashEmbedder
from app.rag.evaluation import evaluate, load_cases, to_markdown
from app.rag.loaders import load_directory
from app.rag.retriever import KnowledgeRetriever
from app.rag.vector_store import VectorStore
from tests.conftest import KB_DIR

DATASET = BACKEND_DIR / "evaluation" / "datasets" / "retrieval_questions.yaml"


def test_the_dataset_points_at_real_documents_and_sections() -> None:
    cases, no_answer = load_cases(DATASET)
    chunks = chunk_documents(load_directory(KB_DIR))
    documents = {c.metadata.document_id for c in chunks}
    sections = {(c.metadata.document_id, c.section) for c in chunks}

    assert len(cases) == 20 and len(no_answer) >= 2
    assert len({case.id for case in cases + no_answer}) == len(cases) + len(no_answer)
    for case in cases:
        assert case.expected and set(case.expected) <= documents, case.id
        if case.section:
            assert any((doc, case.section) in sections for doc in case.expected), case.id


async def test_offline_retrieval_clears_the_gate(kb_store: VectorStore) -> None:
    cases, no_answer = load_cases(DATASET)
    report = await evaluate(
        KnowledgeRetriever(kb_store, HashEmbedder()), cases, no_answer, as_of=date(2026, 9, 30)
    )

    # Measured offline: hit@5 0.95, MRR 0.81. Real embeddings should do at least as well.
    assert report.hit_at_k >= 0.9
    assert report.mrr >= 0.75
    assert report.no_answer_pass_rate == 1.0
    assert "| hit@5 | 0.95 (PASS" in to_markdown(report, min_hit=0.8)
