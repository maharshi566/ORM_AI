import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    # _env_file=None: tests never read your local .env file.
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="postgresql+asyncpg://test:test@localhost:5432/orm_ai_test",
        redis_url="redis://localhost:6379/15",
        cors_origins="http://localhost:3000",
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI):
    with TestClient(app) as test_client:
        yield test_client


async def _healthy() -> None:
    return None


async def _down() -> None:
    raise ConnectionError("connection refused")


@pytest.fixture
def all_dependencies_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.health.check_database", _healthy)
    monkeypatch.setattr("app.services.health.check_redis", _healthy)


@pytest.fixture
def redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.health.check_database", _healthy)
    monkeypatch.setattr("app.services.health.check_redis", _down)
