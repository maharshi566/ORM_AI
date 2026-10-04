"""Blocking work must never run on the event loop.

ChromaDB, file reading and the data generator are all blocking. If one of them ran
directly inside ``async def`` code, every other request would wait for it. These
tests wrap the blocking functions so that calling them on the event loop's thread
fails; the async code paths must call them in a worker thread (``asyncio.to_thread``).
"""

import asyncio
import functools
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.config.settings import Settings
from app.main import create_app
from app.models import Base
from app.rag import factory, ingestion, retriever
from app.rag.embeddings import HashEmbedder
from app.rag.ingestion import ingest_knowledge_base
from app.rag.retriever import KnowledgeRetriever
from app.rag.vector_store import AsyncVectorStore, SearchFilters, VectorStore
from tests.conftest import KB_DIR

STORE_CALLS = [
    "add_documents",
    "delete_ids",
    "delete_document",
    "update_document",
    "reset",
    "search_documents",
    "get_chunks",
    "ids_by_group",
    "ids",
    "count",
    "fingerprint",
]


def forbid_on_event_loop(monkeypatch: pytest.MonkeyPatch, owner: object, *names: str) -> None:
    """Make ``owner.<name>`` raise when it is called on the thread running the event loop."""
    loop_thread = threading.get_ident()  # the tests below are async, so this is the loop
    for name in names:
        original = getattr(owner, name)

        @functools.wraps(original)
        def guarded(*args, _original=original, _name=name, **kwargs):  # type: ignore[no-untyped-def]
            if threading.get_ident() == loop_thread:
                raise AssertionError(f"{_name}() is blocking and ran on the event loop")
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, name, guarded)


def test_the_guard_itself_works(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Called from the main thread it must raise: this proves the other tests can fail.
    store = VectorStore(tmp_path / "chroma", "hash-512")
    forbid_on_event_loop(monkeypatch, VectorStore, "count")
    with pytest.raises(AssertionError, match="blocking"):
        store.count()


async def test_async_store_calls_chromadb_in_a_worker_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    forbid_on_event_loop(monkeypatch, VectorStore, *STORE_CALLS)
    store = AsyncVectorStore(VectorStore(tmp_path / "chroma", "hash-512"))

    assert await store.count() == {}
    assert await store.ids() == set()
    assert await store.get_chunks() == []
    assert await store.search_documents([0.0] * 512, filters=SearchFilters()) == []
    assert await store.fingerprint()


async def test_ingestion_never_blocks_the_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    forbid_on_event_loop(monkeypatch, VectorStore, *STORE_CALLS)
    forbid_on_event_loop(monkeypatch, ingestion, "load_directory", "chunk_documents")
    kb = tmp_path / "kb"
    shutil.copytree(KB_DIR, kb)
    embedder = HashEmbedder()
    store = VectorStore(tmp_path / "chroma", embedder.model)

    report = await ingest_knowledge_base(kb_dir=kb, store=store, embedder=embedder, rebuild=True)
    again = await ingest_knowledge_base(kb_dir=kb, store=store, embedder=embedder)

    assert report.added == report.chunks and again.added == 0


async def test_searching_never_blocks_the_loop(
    monkeypatch: pytest.MonkeyPatch, kb_store: VectorStore
) -> None:
    forbid_on_event_loop(monkeypatch, VectorStore, *STORE_CALLS)

    class GuardedIndex(retriever.KeywordIndex):
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            if threading.get_ident() == loop_thread:
                raise AssertionError("building the keyword index ran on the event loop")
            super().__init__(*args, **kwargs)

    loop_thread = threading.get_ident()
    monkeypatch.setattr(retriever, "KeywordIndex", GuardedIndex)

    search = KnowledgeRetriever(kb_store, HashEmbedder())
    result = await search.search("household credit limit", k=3)

    assert result.found


async def test_the_search_endpoint_never_blocks_the_loop(
    monkeypatch: pytest.MonkeyPatch, kb_store: VectorStore
) -> None:
    forbid_on_event_loop(monkeypatch, VectorStore, *STORE_CALLS)
    loop_thread = threading.get_ident()
    built_on: list[int] = []

    def counting_build(*args, **kwargs):  # type: ignore[no-untyped-def]
        built_on.append(threading.get_ident())
        return factory.build_retriever(*args, **kwargs)

    monkeypatch.setattr("app.api.routes.knowledge.build_retriever", counting_build)
    settings = Settings(
        _env_file=None,
        app_env="test",
        embedding_model="hash",
        chroma_persist_dir=str(kb_store.persist_dir),
        business_date="2026-09-30",
    )
    app = create_app(settings)

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        # Two first requests at the same time must share one retriever.
        first, second = await asyncio.gather(
            client.get("/api/knowledge/search", params={"q": "household credit limit"}),
            client.get("/api/knowledge/search", params={"q": "payment reminder rules"}),
        )

    assert first.status_code == second.status_code == 200
    assert first.json()["found"] and second.json()["found"]
    assert len(built_on) == 1  # built once, not once per request
    assert built_on[0] != loop_thread  # and in a worker thread


async def test_seed_script_never_blocks_the_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts import seed

    monkeypatch.setattr(seed, "BACKEND_DIR", tmp_path)  # do not touch the repo's data folder
    forbid_on_event_loop(monkeypatch, seed, "generate", "validate", "_write_files")
    url = f"sqlite+aiosqlite:///{tmp_path / 'seed.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    args = SimpleNamespace(
        reset=False,
        dry_run=False,
        seed=42,
        anchor=seed.DEFAULT_ANCHOR,
        days=seed.DEFAULT_DAYS,
        export_dir=tmp_path / "csv",
        database_url=url,
    )

    assert await seed.run(args) == 0  # type: ignore[arg-type]
    assert (tmp_path / "data" / "seed" / "EDGE_CASES.md").is_file()
    assert (tmp_path / "csv" / "shops.csv").is_file()
