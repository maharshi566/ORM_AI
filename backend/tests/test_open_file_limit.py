"""ChromaDB forgets search indexes when the open-file limit is small.

ChromaDB sizes its in-memory index cache as (open-file limit // 5). Windows reports 512
and macOS 256, so the cache is tiny, indexes push each other out, and an index that was
never saved is lost: "Error creating hnsw segment reader: Nothing found on disk".
``app/rag/open_files.py`` raises the limit before the client is created. These tests cover
the raising logic, the order of the calls, and the real symptom in a fresh process.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config.paths import BACKEND_DIR
from app.rag import open_files
from app.rag.open_files import ENOUGH, raise_open_file_limit
from app.rag.vector_store import LOST_INDEX, VectorStore, VectorStoreError

# ------------------------------------------------------- Linux and macOS logic


class FakeResource:
    """Stands in for the ``resource`` module: two limits and the highest value allowed."""

    RLIMIT_NOFILE = 7
    RLIM_INFINITY = -1

    def __init__(self, soft: int, hard: int, highest: int | None = None) -> None:
        self.limits = (soft, hard)
        self.highest = highest  # macOS refuses anything above 10,240
        self.attempts: list[int] = []

    def getrlimit(self, which: int) -> tuple[int, int]:
        return self.limits

    def setrlimit(self, which: int, limits: tuple[int, int]) -> None:
        soft, hard = limits
        self.attempts.append(soft)
        if self.highest is not None and soft > self.highest:
            raise ValueError("current limit exceeds maximum limit")
        if self.limits[1] != self.RLIM_INFINITY and soft > self.limits[1]:
            raise ValueError("current limit exceeds maximum limit")
        self.limits = (soft, hard)


def test_a_small_linux_limit_is_raised_to_the_highest_value_allowed() -> None:
    fake = FakeResource(soft=1024, hard=1_048_576)

    before, after = open_files._raise_posix(fake)

    assert (before, after) == (1024, 65536)
    assert fake.limits == (65536, 1_048_576)  # only the soft limit moves


def test_macos_refuses_huge_limits_so_the_next_size_down_is_used() -> None:
    fake = FakeResource(soft=256, hard=-1, highest=10240)  # hard limit: unlimited

    before, after = open_files._raise_posix(fake)

    assert (before, after) == (256, 10240)
    assert fake.attempts == [65536, 16384, 10240]


def test_a_limit_that_is_already_high_enough_is_left_alone() -> None:
    fake = FakeResource(soft=ENOUGH, hard=1_048_576)

    assert open_files._raise_posix(fake) == (ENOUGH, ENOUGH)
    assert fake.attempts == []


def test_an_unlimited_limit_is_left_alone() -> None:
    fake = FakeResource(soft=-1, hard=-1)

    assert open_files._raise_posix(fake) == (-1, -1)
    assert fake.attempts == []


def test_a_low_hard_limit_is_used_as_far_as_it_goes() -> None:
    fake = FakeResource(soft=256, hard=900)

    assert open_files._raise_posix(fake) == (256, 900)
    assert fake.limits == (900, 900)


def test_the_limit_is_never_lowered() -> None:
    fake = FakeResource(soft=100_000, hard=200_000)

    assert open_files._raise_posix(fake) == (100_000, 100_000)
    assert fake.limits == (100_000, 200_000)


# ------------------------------------------------------------- Windows logic


class FakeCrt:
    """Stands in for msvcrt.dll: ``_setmaxstdio`` returns the new limit, or -1 if refused."""

    def __init__(self, current: int, highest: int) -> None:
        self.current = current
        self.highest = highest
        self.attempts: list[int] = []

    def _getmaxstdio(self) -> int:
        return self.current

    def _setmaxstdio(self, new_max: int) -> int:
        self.attempts.append(new_max)
        if 32 <= new_max <= self.highest:
            self.current = new_max
            return new_max
        return -1


def test_windows_default_of_512_is_raised_to_the_most_it_allows() -> None:
    crt = FakeCrt(current=512, highest=2048)  # some Windows versions stop at 2,048

    assert open_files._raise_windows(crt) == (512, 2048)
    assert crt.attempts == [8192, 4096, 2048]  # tries the biggest first


def test_windows_allowing_8192_gets_8192() -> None:
    assert open_files._raise_windows(FakeCrt(current=512, highest=8192)) == (512, 8192)


def test_windows_refusing_everything_keeps_the_default() -> None:
    crt = FakeCrt(current=512, highest=512)

    assert open_files._raise_windows(crt) == (512, 512)


def test_windows_limit_that_is_already_high_enough_is_left_alone() -> None:
    crt = FakeCrt(current=4096, highest=8192)

    assert open_files._raise_windows(crt) == (4096, 4096)
    assert crt.attempts == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_on_real_windows_the_limit_chromadb_reads_goes_up() -> None:
    import ctypes

    raise_open_file_limit()

    # ChromaDB reads exactly this number when it starts.
    assert ctypes.windll.msvcrt._getmaxstdio() >= ENOUGH


def test_a_failure_while_raising_the_limit_never_stops_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object) -> None:
        raise OSError("not allowed")

    monkeypatch.setattr(open_files, "_raise_posix", refuse)
    monkeypatch.setattr(open_files, "_raise_windows", refuse)

    assert raise_open_file_limit() is None


# ------------------------------------------------------------ the vector store


def test_the_limit_is_raised_before_the_chromadb_client_is_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ChromaDB reads the limit once, when the client is created, so the order matters.
    calls: list[str] = []
    monkeypatch.setattr(
        "app.rag.vector_store.raise_open_file_limit", lambda: calls.append("raise limit")
    )
    monkeypatch.setattr(
        "app.rag.vector_store.chromadb.PersistentClient",
        lambda **kwargs: calls.append("create client"),
    )

    VectorStore(tmp_path, "some-model")

    assert calls == ["raise limit", "create client"]


class BrokenClient:
    def __init__(self, message: str) -> None:
        self.message = message

    def list_collections(self) -> list[str]:
        raise RuntimeError(self.message)


def test_chromadb_errors_keep_their_reason(tmp_path: Path) -> None:
    store = VectorStore(tmp_path, "m", client=BrokenClient("database is locked"))  # type: ignore[arg-type]

    with pytest.raises(VectorStoreError) as caught:
        store.count()

    message = str(caught.value)
    assert "could not count chunks (RuntimeError): database is locked." in message
    assert "restart" not in message  # that advice is only for the lost-index error


def test_the_lost_index_error_says_what_to_do(tmp_path: Path) -> None:
    reason = (
        f"Error executing plan: Internal error: Error creating hnsw segment reader: {LOST_INDEX}"
    )
    store = VectorStore(tmp_path, "m", client=BrokenClient(reason))  # type: ignore[arg-type]

    with pytest.raises(VectorStoreError) as caught:
        store.count()

    assert LOST_INDEX in str(caught.value)
    assert "restart the backend" in str(caught.value)


# ----------------------------------------------- the real symptom, in a fresh process

CHILD = r"""
import asyncio
import sys
import tempfile
from pathlib import Path

if sys.platform != "win32":
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(512, soft), hard))  # what Windows reports

from app.config.paths import BACKEND_DIR
from app.rag.embeddings import HashEmbedder
from app.rag.ingestion import ingest_knowledge_base
from app.rag.vector_store import VectorStore


async def main() -> None:
    embedder = HashEmbedder()
    vector = await embedder.embed_query("credit limit for a household customer")
    folder = Path(tempfile.mkdtemp())
    # Four copies of the knowledge base (as if four embedding models) in one database:
    # 20 collections, far more than ChromaDB's cache can hold at a limit of 512.
    stores = [VectorStore(folder, f"hash-512-copy{n}") for n in range(4)]
    for store in stores:
        await ingest_knowledge_base(
            kb_dir=BACKEND_DIR / "knowledge_base", store=store, embedder=embedder
        )
    for store in stores:
        store.count()  # the first touch builds each collection's index in memory
    for store in stores:
        assert store.search_documents(vector, k=3), "search found nothing"


asyncio.run(main())
print("every collection could be searched")
"""


def test_every_index_survives_a_windows_sized_open_file_limit() -> None:
    """Without the fix this fails in about 19 runs out of 20 (it needs bad luck to pass)."""
    paths = [str(BACKEND_DIR), os.environ.get("PYTHONPATH", "")]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, paths))}

    done = subprocess.run(  # noqa: S603  (our own interpreter and our own script)
        [sys.executable, "-c", CHILD],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )

    assert done.returncode == 0, done.stderr[-1500:]
    assert "every collection could be searched" in done.stdout
