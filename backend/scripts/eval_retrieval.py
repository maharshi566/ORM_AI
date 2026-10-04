"""Measure retrieval quality on the 20-question test set (the Phase 3 gate).

Usage (from backend/, after ``python -m scripts.ingest``):

    python -m scripts.eval_retrieval                          # uses EMBEDDING_MODEL
    python -m scripts.eval_retrieval --embedding-model hash   # offline baseline

Prints hit@5, hit@1, MRR and every miss, and writes a Markdown report to
evaluation/reports/. Exits with code 1 when hit@5 is below the gate (0.8).
"""

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

from app.config.paths import BACKEND_DIR, backend_path
from app.config.settings import get_settings
from app.core.logging import configure_logging
from app.rag.embeddings import EmbeddingConfigError
from app.rag.evaluation import evaluate, load_cases, to_markdown
from app.rag.factory import build_retriever
from app.rag.retriever import KnowledgeBaseEmptyError

DATASET = BACKEND_DIR / "evaluation" / "datasets" / "retrieval_questions.yaml"
REPORTS = BACKEND_DIR / "evaluation" / "reports"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--embedding-model", help="override EMBEDDING_MODEL (e.g. hash)")
    parser.add_argument("--k", type=int, default=5, help="results per question (default 5)")
    parser.add_argument("--min-hit", type=float, default=0.8, help="gate for hit@k (default 0.8)")
    parser.add_argument("--dataset", default=str(DATASET), help="questions YAML file")
    parser.add_argument("--chroma-dir", help="ChromaDB folder (default CHROMA_PERSIST_DIR)")
    return parser.parse_args(argv)


def _write_report(path: Path, markdown: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8", newline="\n")  # LF on every OS


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging("WARNING", json_logs=settings.log_json)
    if args.chroma_dir:
        settings = settings.model_copy(update={"chroma_persist_dir": args.chroma_dir})
    try:
        retriever = await asyncio.to_thread(
            build_retriever, settings, embedding_model=args.embedding_model
        )
    except EmbeddingConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    cases, no_answer = await asyncio.to_thread(load_cases, backend_path(args.dataset))
    try:
        report = await evaluate(
            retriever, cases, no_answer, k=args.k, as_of=settings.business_date or date.today()
        )
    except KnowledgeBaseEmptyError as exc:
        print(exc, file=sys.stderr)
        return 2

    print(f"Embedding model {report.embedding_model}, reranker {report.reranker}")
    print(f"  hit@{args.k}:  {report.hit_at_k:.2f}   (gate {args.min_hit:.2f})")
    print(f"  hit@1:  {report.hit_at_1:.2f}")
    print(f"  MRR:    {report.mrr:.2f}")
    print(f"  section hit: {report.section_hit_rate:.2f}")
    print(f"  off-topic questions answered with nothing: {report.no_answer_pass_rate:.2f}")
    for result in report.results:
        if not result.hit:
            got = ", ".join(result.document_ids) or "nothing"
            print(f"  MISS {result.case.id}: {result.case.question}")
            print(f"       expected {', '.join(result.case.expected)}; got {got}")
    for item in report.no_answer:
        if item.citations:
            print(f"  OFF-TOPIC {item.case.id} returned {', '.join(item.citations)}")
    if hint := report.calibration():
        print(f"  {hint}")

    slug = report.embedding_model.replace("/", "-")
    path = REPORTS / f"retrieval-{slug}.md"
    await asyncio.to_thread(_write_report, path, to_markdown(report, min_hit=args.min_hit))
    print(f"Report: {path.relative_to(BACKEND_DIR)}")
    passed = report.hit_at_k >= args.min_hit
    print("PASS" if passed else "FAIL: below the gate")
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
