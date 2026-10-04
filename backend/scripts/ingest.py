"""Embed the knowledge base into ChromaDB and record it in the database.

Usage (from backend/, with the virtual environment active):

    python -m scripts.ingest                         # add new/changed chunks, drop deleted
    python -m scripts.ingest --dry-run               # load and chunk only; embed nothing
    python -m scripts.ingest --rebuild               # delete this model's vectors, re-embed
    python -m scripts.ingest --embedding-model hash  # offline trial, no OpenAI key needed
    python -m scripts.ingest --skip-db               # vectors only; leave the tables alone

Run it again whenever you add or edit a file in knowledge_base/. Unchanged chunks
are skipped, so a re-run costs nothing.
"""

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config.paths import BACKEND_DIR, backend_path
from app.config.settings import get_settings
from app.core.logging import configure_logging
from app.models.database import make_engine
from app.rag.embeddings import EmbeddingConfigError, EmbeddingError, build_embedder
from app.rag.ingestion import IngestReport, ingest_knowledge_base
from app.rag.loaders import LoaderError
from app.rag.vector_store import VectorStore, VectorStoreError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="load and chunk only")
    parser.add_argument("--rebuild", action="store_true", help="re-embed every chunk")
    parser.add_argument("--embedding-model", help="override EMBEDDING_MODEL (e.g. hash)")
    parser.add_argument("--skip-db", action="store_true", help="do not update the database")
    parser.add_argument("--kb-dir", help="knowledge-base folder (default KNOWLEDGE_BASE_DIR)")
    parser.add_argument("--chroma-dir", help="ChromaDB folder (default CHROMA_PERSIST_DIR)")
    return parser.parse_args(argv)


def format_report(report: IngestReport, chroma_dir: str) -> str:
    lines = [f"Knowledge base: {report.documents} documents -> {report.chunks} chunks"]
    if report.dry_run:
        lines.append(f"  {'Collection':<12}{'Chunks':>8}")
        for name, stats in report.by_group.items():
            lines.append(f"  {name:<12}{stats.chunks:>8}")
        lines.append("Dry run: nothing was embedded or stored.")
        return "\n".join(lines)
    lines.append(f"Embedding model: {report.embedding_model}    Vectors: {chroma_dir}")
    lines.append(f"  {'Collection':<12}{'Chunks':>8}{'Added':>8}{'Unchanged':>11}{'Removed':>9}")
    for name, stats in report.by_group.items():
        lines.append(
            f"  {name:<12}{stats.chunks:>8}{stats.added:>8}{stats.unchanged:>11}{stats.removed:>9}"
        )
    lines.append(
        f"  {'total':<12}{report.chunks:>8}{report.added:>8}{report.unchanged:>11}"
        f"{report.removed:>9}"
    )
    if report.added:
        lines.append(f"Embedded about {report.embedded_tokens:,} tokens.")
    else:
        lines.append("Nothing new to embed: every chunk was already stored.")
    lines.append(f"Database: {report.database}.")
    lines.append(f"Done in {report.seconds:.1f} s.")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)
    kb_dir = backend_path(args.kb_dir or settings.knowledge_base_dir)
    chroma_dir = backend_path(args.chroma_dir or settings.chroma_persist_dir)
    shown_dir = (
        chroma_dir.relative_to(BACKEND_DIR)
        if chroma_dir.is_relative_to(BACKEND_DIR)
        else chroma_dir
    )

    if args.dry_run:
        try:
            report = await ingest_knowledge_base(
                kb_dir=kb_dir, store=None, embedder=None, dry_run=True
            )
        except LoaderError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(format_report(report, str(shown_dir)))
        return 0

    try:
        embedder = build_embedder(settings, args.embedding_model)
    except EmbeddingConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    store = VectorStore(chroma_dir, embedder.model)
    engine = None
    session_factory = None
    if not args.skip_db:
        engine = make_engine(
            settings.database_url, transaction_pooler=settings.db_transaction_pooler
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        report = await ingest_knowledge_base(
            kb_dir=kb_dir,
            store=store,
            embedder=embedder,
            session_factory=session_factory,
            rebuild=args.rebuild,
        )
    except LoaderError as exc:
        print(exc, file=sys.stderr)
        return 1
    except EmbeddingError as exc:
        print(
            f"Embedding failed: {exc}\nChunks stored before the failure are kept; "
            "run the command again to continue.",
            file=sys.stderr,
        )
        return 1
    except VectorStoreError as exc:
        print(f"{exc} Check that {shown_dir} is writable and not open elsewhere.", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            await engine.dispose()

    print(format_report(report, str(shown_dir)))
    if report.database_failed:
        print(
            "The vectors are stored, but the database was not updated. Start PostgreSQL "
            "(docker compose up -d postgres) and run again, or use --skip-db.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
