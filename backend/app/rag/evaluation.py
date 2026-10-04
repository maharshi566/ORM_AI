"""Retrieval evaluation: how often does search find the right document?

Metrics over the questions in ``evaluation/datasets/retrieval_questions.yaml``:

* **hit@k**: share of questions where an expected document is in the top ``k``
  (the Phase 3 gate is hit@5 >= 0.8).
* **hit@1**: the expected document comes first.
* **MRR** (mean reciprocal rank): 1 for first place, 1/2 for second, ... 0 if
  missing, averaged. Rewards ranking the answer high, not just somewhere.
* **section hit**: when a question names its best section, is that section in the
  top ``k``?
* **no-answer pass rate**: off-topic questions that correctly return nothing.
"""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from app.rag.retriever import KnowledgeRetriever
from app.rag.vector_store import SearchFilters


@dataclass(frozen=True)
class RetrievalCase:
    id: str
    question: str
    expected: tuple[str, ...] = ()
    section: str | None = None
    shop_id: str | None = None


@dataclass
class CaseResult:
    case: RetrievalCase
    citations: list[str]
    document_ids: list[str]
    rank: int | None  # 1-based rank of the first expected document, None if missing
    section_hit: bool | None  # None when the case names no section
    keyword_only: bool = False
    answer_similarity: float | None = None  # meaning-search score of the expected chunk

    @property
    def hit(self) -> bool:
        return self.rank is not None


@dataclass
class NoAnswerResult:
    case: RetrievalCase
    citations: list[str]
    best_similarity: float | None  # how close the nearest chunk came, before the gate


@dataclass
class EvalReport:
    embedding_model: str
    reranker: str
    k: int
    min_similarity: float = 0.0
    results: list[CaseResult] = field(default_factory=list)
    no_answer: list[NoAnswerResult] = field(default_factory=list)

    def _share(self, values: list[bool]) -> float:
        return sum(values) / len(values) if values else 0.0

    @property
    def hit_at_k(self) -> float:
        return self._share([r.hit for r in self.results])

    @property
    def hit_at_1(self) -> float:
        return self._share([r.rank == 1 for r in self.results])

    @property
    def mrr(self) -> float:
        ranks = [1 / r.rank if r.rank else 0.0 for r in self.results]
        return sum(ranks) / len(ranks) if ranks else 0.0

    @property
    def section_hit_rate(self) -> float:
        return self._share([r.section_hit for r in self.results if r.section_hit is not None])

    @property
    def no_answer_pass_rate(self) -> float:
        return self._share([not item.citations for item in self.no_answer])

    def calibration(self) -> str | None:
        """Where RETRIEVAL_MIN_SIMILARITY should sit, judged from this run."""
        answers = [r.answer_similarity for r in self.results if r.answer_similarity is not None]
        noise = [n.best_similarity for n in self.no_answer if n.best_similarity is not None]
        if not answers or not noise:
            return None
        return (
            f"Similarity of correct answers: lowest {min(answers):.2f}, median "
            f"{sorted(answers)[len(answers) // 2]:.2f}. Off-topic questions: highest "
            f"{max(noise):.2f}. Current threshold: {self.min_similarity:.2f}. A value "
            "between the off-topic highest and the answers' median drops noise and keeps "
            "answers (questions that share words with an answer are kept by keyword search)."
        )


def load_cases(path: Path) -> tuple[list[RetrievalCase], list[RetrievalCase]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    def case(item: dict) -> RetrievalCase:
        return RetrievalCase(
            id=str(item["id"]),
            question=str(item["question"]),
            expected=tuple(item.get("expected", [])),
            section=item.get("section"),
            shop_id=item.get("shop_id"),
        )

    return [case(i) for i in data.get("questions", [])], [
        case(i) for i in data.get("no_answer", [])
    ]


async def evaluate(
    retriever: KnowledgeRetriever,
    cases: list[RetrievalCase],
    no_answer: list[RetrievalCase],
    *,
    k: int = 5,
    as_of: date | None = None,
) -> EvalReport:
    report = EvalReport(
        retriever.store.embedding_model, retriever.reranker.name, k, retriever.min_similarity
    )
    for case in cases:
        filters = SearchFilters(as_of=as_of, shop_id=case.shop_id)
        result = await retriever.search(case.question, k=k, filters=filters)
        doc_ids = [chunk.document_id for chunk in result.chunks]
        rank = next((i for i, d in enumerate(doc_ids, start=1) if d in case.expected), None)
        section_hit = None
        if case.section:
            section_hit = any(
                chunk.section == case.section and chunk.document_id in case.expected
                for chunk in result.chunks
            )
        report.results.append(
            CaseResult(
                case,
                [chunk.citation for chunk in result.chunks],
                doc_ids,
                rank,
                section_hit,
                result.keyword_only,
                result.chunks[rank - 1].similarity if rank else None,
            )
        )
    for case in no_answer:
        result = await retriever.search(case.question, k=k, filters=SearchFilters(as_of=as_of))
        report.no_answer.append(
            NoAnswerResult(case, [c.citation for c in result.chunks], result.best_similarity)
        )
    return report


def to_markdown(report: EvalReport, *, min_hit: float) -> str:
    verdict = "PASS" if report.hit_at_k >= min_hit else "FAIL"
    lines = [
        "# Retrieval evaluation",
        "",
        f"Embedding model `{report.embedding_model}`, reranker `{report.reranker}`, "
        f"k = {report.k}.",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| hit@{report.k} | {report.hit_at_k:.2f} ({verdict}: gate {min_hit:.2f}) |",
        f"| hit@1 | {report.hit_at_1:.2f} |",
        f"| MRR | {report.mrr:.2f} |",
        f"| section hit | {report.section_hit_rate:.2f} |",
        f"| no-answer pass rate | {report.no_answer_pass_rate:.2f} |",
        "",
        "| Case | Result | Rank | Question | Top results |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in report.results:
        top = "<br>".join(r.citations[:3]) or "(nothing)"
        lines.append(
            f"| {r.case.id} | {'hit' if r.hit else 'MISS'} | {r.rank or '-'} | "
            f"{r.case.question} | {top} |"
        )
    lines += ["", "| Off-topic case | Result | Returned |", "| --- | --- | --- |"]
    for item in report.no_answer:
        result = "pass" if not item.citations else "FAIL"
        returned = ", ".join(item.citations) or "-"
        lines.append(f"| {item.case.id} {item.case.question} | {result} | {returned} |")
    if hint := report.calibration():
        lines += ["", hint]
    return "\n".join(lines) + "\n"
