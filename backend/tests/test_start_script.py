"""The container's start command (scripts/start.py): what runs, in which order."""

import sys

import pytest

from scripts import start


class Recorder:
    def __init__(self) -> None:
        self.ran: list[list[str]] = []
        self.served: list[str] | None = None


@pytest.fixture
def recorder(monkeypatch) -> Recorder:
    record = Recorder()
    for name in ("SEED_IF_EMPTY", "INGEST_ON_START", "PORT", "FORWARDED_ALLOW_IPS"):
        monkeypatch.delenv(name, raising=False)

    def fake_run(command, check):  # noqa: ARG001
        record.ran.append(command[2:])  # without "python -m"

    def fake_execv(path, command):  # noqa: ARG001
        record.served = command[2:]

    monkeypatch.setattr(start.subprocess, "run", fake_run)
    monkeypatch.setattr(start.os, "execv", fake_execv)
    return record


def empty_database(empty: bool):
    async def check() -> bool:
        return empty

    return check


def test_by_default_it_migrates_and_serves_as_before(recorder, monkeypatch) -> None:
    start.main()

    assert recorder.ran == [["alembic", "upgrade", "head"]]
    served = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]  # noqa: S104
    assert recorder.served == served


def test_the_demo_shops_go_only_into_an_empty_database(recorder, monkeypatch) -> None:
    monkeypatch.setenv("SEED_IF_EMPTY", "true")
    monkeypatch.setattr(start, "database_is_empty", empty_database(True))
    start.main()
    seeded = list(recorder.ran)

    recorder.ran.clear()
    monkeypatch.setattr(start, "database_is_empty", empty_database(False))
    start.main()

    assert seeded == [["alembic", "upgrade", "head"], ["scripts.seed"]]
    assert recorder.ran == [["alembic", "upgrade", "head"]]  # shop data is never touched


def test_a_host_without_a_disk_rebuilds_the_index_and_picks_the_port(recorder, monkeypatch) -> None:
    monkeypatch.setenv("INGEST_ON_START", "1")
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")

    start.main()

    assert recorder.ran[-1] == ["scripts.ingest"]
    assert recorder.served is not None
    assert recorder.served[recorder.served.index("--port") + 1] == "10000"
    assert recorder.served[-3:] == ["--proxy-headers", "--forwarded-allow-ips", "*"]


def test_it_starts_this_python(recorder, monkeypatch) -> None:
    paths: list[str] = []
    monkeypatch.setattr(start.os, "execv", lambda path, command: paths.append(path))

    start.main()

    assert paths == [sys.executable]


async def test_a_database_with_only_a_user_is_not_seeded(tmp_path, monkeypatch) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config.settings import Settings
    from app.models import Base, User

    url = f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(
        "app.config.settings.get_settings", lambda: Settings(_env_file=None, database_url=url)
    )

    empty = await start.database_is_empty()
    async with engine.begin() as conn:
        await conn.execute(User.__table__.insert().values(id="USR-001", name="A", role="admin"))
    with_a_user = await start.database_is_empty()
    await engine.dispose()

    assert empty is True and with_a_user is False
