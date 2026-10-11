"""Phase 9: what a deployment needs from the backend.

* A hosting provider's database URL (``postgres://...?sslmode=require``) works as given.
* A public demo can open the login for a short list of demo accounts, never an admin,
  while every other login still needs the deployment's own tokens.
"""

import pytest

from app.config.settings import Settings
from tests.test_api_phase6 import make_client  # noqa: F401

PRODUCTION = {
    "app_env": "production",
    "auth_required": True,
    "cors_origins": "https://orm-ai.example",
    "auth_demo_users": "USR-003, usr-004, USR-101",
}


@pytest.mark.parametrize(
    ("given", "used"),
    [
        # Render, Railway, Neon and Heroku
        (
            "postgres://u:p@db.host:5432/orm?sslmode=require",
            "postgresql+asyncpg://u:p@db.host:5432/orm?ssl=require",
        ),
        ("postgresql://u:p@db.host/orm", "postgresql+asyncpg://u:p@db.host/orm"),
        (
            "postgresql+asyncpg://u:p@db.host/orm?application_name=x&sslmode=verify-full",
            "postgresql+asyncpg://u:p@db.host/orm?application_name=x&ssl=verify-full",
        ),
        # Already right, or not PostgreSQL: unchanged
        (
            "postgresql+asyncpg://u:p@db.host/orm?ssl=require",
            "postgresql+asyncpg://u:p@db.host/orm?ssl=require",
        ),
        ("sqlite+aiosqlite:///./demo.db", "sqlite+aiosqlite:///./demo.db"),
    ],
)
def test_a_hosting_providers_database_url_works_as_given(given: str, used: str) -> None:
    assert Settings(_env_file=None, database_url=given).database_url == used


async def test_a_public_demo_lets_only_its_accounts_log_in(make_client) -> None:  # noqa: F811
    api = make_client(**PRODUCTION)

    listed = (await api.get("/api/auth/dev-users")).json()
    owner = await api.post("/api/auth/dev-token", json={"user_id": "USR-003"})
    not_listed = await api.post("/api/auth/dev-token", json={"user_id": "USR-001"})
    admin = await api.post("/api/auth/dev-token", json={"user_id": "USR-101"})

    assert [u["user_id"] for u in listed] == ["USR-003", "USR-004"]  # the admin is left out
    assert owner.status_code == 200 and owner.json()["shop_id"] == "SHOP-002"
    assert not_listed.status_code == 403
    assert admin.status_code == 403 and "admin" in admin.json()["error"]["message"]
    # The demo token is an ordinary login: logins are required, and it reaches its shop.
    headers = {"Authorization": f"Bearer {owner.json()['access_token']}"}
    assert (await api.get("/api/auth/me", headers=headers)).json()["user_id"] == "USR-003"
    assert (await api.get("/api/workflows")).status_code == 401


async def test_without_demo_accounts_a_shared_server_refuses_demo_logins(make_client) -> None:  # noqa: F811
    api = make_client(**{**PRODUCTION, "auth_demo_users": ""})

    assert (await api.get("/api/auth/dev-users")).status_code == 403
    assert (await api.post("/api/auth/dev-token", json={"user_id": "USR-003"})).status_code == 403


def test_rate_limits_can_be_kept_in_redis_with_the_installed_client() -> None:
    """RATE_LIMIT_STORAGE=redis://... once needed a package that was never installed, so
    every rate-limited request failed with a 500 (the production stack's smoke test
    found it). It now uses redis-py, already installed; nothing connects until a count."""
    from limits.aio.storage import MemoryStorage, RedisStorage

    from app.core.rate_limit import _storage

    redis = _storage(Settings(_env_file=None, rate_limit_storage="redis://localhost:6379/1"))
    memory = _storage(Settings(_env_file=None, rate_limit_storage="memory"))

    assert isinstance(redis, RedisStorage) and isinstance(memory, MemoryStorage)
