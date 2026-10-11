"""Real logins: email + password, one-time codes, the team, and sessions that end."""

import pytest
from sqlalchemy import select

from app.config.settings import Settings
from app.core.passwords import password_problems
from app.models import LoginCode, User
from app.services.auth_service import (
    CODE_MAX_ATTEMPTS,
    WRONG_CODE,
    WRONG_PASSWORD,
    ConsoleCodeSender,
)
from tests.test_api_phase6 import make_client, token_for  # noqa: F401  (fixture)

PASSWORD = "chai-and-biscuits-42"
SEED_OWNER_PHONE = "+91-00000-40001"  # USR-001, the owner of SHOP-001 in the seed data


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def add_staff(api, owner: dict, **fields) -> dict:
    body = {"name": "Kavya Shetty", "email": "kavya@example.com", "password": PASSWORD, **fields}
    response = await api.post("/api/users", json=body, headers=owner)
    assert response.status_code == 201, response.text
    return response.json()


async def login(api, email: str, password: str):
    return await api.post("/api/auth/login", json={"email": email, "password": password})


@pytest.fixture
def api(make_client):  # noqa: F811
    return make_client(rate_limit_enabled=False)


# ------------------------------------------------------------------ what is offered


async def test_the_login_page_learns_what_this_server_offers(make_client) -> None:  # noqa: F811
    local = (await make_client(rate_limit_enabled=False).get("/api/auth/methods")).json()
    shared = make_client(app_env="staging", auth_required=True, rate_limit_enabled=False)

    assert local == {"password": True, "otp": True, "otp_channel": "console", "demo": True}
    assert (await shared.get("/api/auth/methods")).json() == {
        "password": True,
        "otp": False,  # off by default anywhere but a developer's machine
        "otp_channel": None,
        "demo": False,
    }


def test_production_refuses_codes_in_the_log_and_whatsapp_without_its_keys() -> None:
    base = {"_env_file": None, "app_env": "production", "auth_required": True}
    base["auth_secret"] = "x" * 40
    console = Settings(**base, otp_channel="console")
    whatsapp = Settings(**base, otp_channel="whatsapp")

    assert any("OTP_CHANNEL=console" in p for p in console.production_problems())
    assert any("WHATSAPP_ACCESS_TOKEN" in p for p in whatsapp.production_problems())
    assert Settings(**base).production_problems() == []  # codes off: nothing missing


# ------------------------------------------------------------------ passwords


async def test_an_owner_adds_staff_who_then_log_in_with_a_password(api) -> None:
    owner = await token_for(api, "USR-001")
    staff = await add_staff(api, owner, email="  Kavya@Example.com ")

    response = await login(api, "kavya@example.com", PASSWORD)

    assert staff["shop_id"] == "SHOP-001" and staff["role"] == "staff"
    assert staff["email"] == "kavya@example.com" and staff["has_password"]
    assert response.status_code == 200, response.text
    me = (await api.get("/api/auth/me", headers=bearer(response.json()["access_token"]))).json()
    assert me["user_id"] == staff["user_id"] and me["shop_id"] == "SHOP-001"


async def test_a_wrong_email_and_a_wrong_password_look_the_same(api) -> None:
    await add_staff(api, await token_for(api, "USR-001"))

    unknown = await login(api, "nobody@example.com", PASSWORD)
    wrong = await login(api, "kavya@example.com", "not-the-password")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"] == WRONG_PASSWORD


async def test_five_wrong_passwords_lock_the_account_for_a_while(api, session_factory) -> None:
    await add_staff(api, await token_for(api, "USR-001"))

    answers = [await login(api, "kavya@example.com", f"wrong-{i}") for i in range(5)]
    while_locked = await login(api, "kavya@example.com", PASSWORD)

    assert [a.status_code for a in answers] == [401, 401, 401, 401, 429]
    assert while_locked.status_code == 429
    assert "Try again in" in while_locked.json()["error"]["message"]
    async with session_factory() as db:
        user = await db.scalar(select(User).where(User.email == "kavya@example.com"))
        assert user.locked_until is not None


@pytest.mark.parametrize(
    ("password", "problem"),
    [
        ("short", "at least 10"),
        ("1234567890", "too common"),
        ("kavyashetty-secret", "email address"),
    ],
)
def test_weak_passwords_are_refused_with_a_reason(password, problem) -> None:
    problems = password_problems(password, email="kavyashetty@example.com")

    assert any(problem in p for p in problems), problems


async def test_a_weak_password_is_refused_when_adding_a_user(api) -> None:
    response = await api.post(
        "/api/users",
        json={"name": "Kavya", "email": "k@example.com", "password": "1234567890"},
        headers=await token_for(api, "USR-001"),
    )

    assert response.status_code == 422 and "too common" in response.json()["error"]["message"]


async def test_changing_the_password_ends_every_other_session(api) -> None:
    await add_staff(api, await token_for(api, "USR-001"))
    old = (await login(api, "kavya@example.com", PASSWORD)).json()["access_token"]

    changed = await api.post(
        "/api/auth/password",
        json={"current_password": PASSWORD, "new_password": "masala-dosa-on-sunday"},
        headers=bearer(old),
    )

    assert changed.status_code == 200, changed.text
    fresh = changed.json()["access_token"]
    assert (await api.get("/api/users", headers=bearer(old))).status_code == 401
    assert (await api.get("/api/auth/me", headers=bearer(fresh))).json()["authenticated"]
    assert (await login(api, "kavya@example.com", "masala-dosa-on-sunday")).status_code == 200


# ------------------------------------------------------------------ one-time codes


async def test_a_code_sent_to_the_phone_logs_the_user_in_once(api) -> None:
    sender = api.app.state.code_sender = ConsoleCodeSender()

    asked = await api.post("/api/auth/otp/request", json={"phone": SEED_OWNER_PHONE})
    [(phone, code)] = sender.sent
    first = await api.post("/api/auth/otp/verify", json={"phone": "+910000040001", "code": code})
    again = await api.post("/api/auth/otp/verify", json={"phone": SEED_OWNER_PHONE, "code": code})

    assert asked.status_code == 202 and asked.json()["channel"] == "console"
    assert phone == "+910000040001" and len(code) == 6
    assert first.status_code == 200 and first.json()["user_id"] == "USR-001"
    assert again.status_code == 401 and again.json()["error"]["message"] == WRONG_CODE  # used up


async def test_asking_for_a_code_does_not_reveal_who_has_an_account(api) -> None:
    sender = api.app.state.code_sender = ConsoleCodeSender()

    known = await api.post("/api/auth/otp/request", json={"phone": SEED_OWNER_PHONE})
    unknown = await api.post("/api/auth/otp/request", json={"phone": "+91 98765 43210"})

    assert known.json() == unknown.json() and known.status_code == unknown.status_code == 202
    assert len(sender.sent) == 1  # only the real account got a code


async def test_codes_cannot_be_asked_for_again_and_again(api, session_factory) -> None:
    sender = api.app.state.code_sender = ConsoleCodeSender()

    for _ in range(3):
        await api.post("/api/auth/otp/request", json={"phone": SEED_OWNER_PHONE})

    assert len(sender.sent) == 1  # one a minute at most
    async with session_factory() as db:
        assert len((await db.scalars(select(LoginCode))).all()) == 1


async def test_too_many_wrong_guesses_burn_the_code(api) -> None:
    sender = api.app.state.code_sender = ConsoleCodeSender()
    await api.post("/api/auth/otp/request", json={"phone": SEED_OWNER_PHONE})
    [(_, code)] = sender.sent
    wrong = "000000" if code != "000000" else "111111"

    guesses = [
        await api.post("/api/auth/otp/verify", json={"phone": SEED_OWNER_PHONE, "code": wrong})
        for _ in range(CODE_MAX_ATTEMPTS)
    ]
    right = await api.post("/api/auth/otp/verify", json={"phone": SEED_OWNER_PHONE, "code": code})

    assert {g.status_code for g in guesses} == {401}
    assert right.status_code == 401  # the right code no longer works either


async def test_codes_are_refused_when_switched_off(make_client) -> None:  # noqa: F811
    api = make_client(rate_limit_enabled=False, otp_channel="off")

    response = await api.post("/api/auth/otp/request", json={"phone": SEED_OWNER_PHONE})

    assert response.status_code == 403


# ------------------------------------------------------------------ the team


async def test_an_owner_manages_only_their_own_staff(api) -> None:
    owner = await token_for(api, "USR-001")

    another_shop = await api.post(
        "/api/users",
        json={"name": "X Y", "email": "x@example.com", "shop_id": "SHOP-002"},
        headers=owner,
    )
    an_owner = await api.post(
        "/api/users",
        json={"name": "X Y", "email": "x@example.com", "role": "owner"},
        headers=owner,
    )
    team = (await api.get("/api/users", headers=owner)).json()

    assert another_shop.status_code == an_owner.status_code == 403
    assert {u["shop_id"] for u in team} == {"SHOP-001"}
    assert all(u["phone"] is None or u["phone"].startswith("+xx") for u in team)  # masked


async def test_staff_cannot_manage_the_team(api) -> None:
    staff = await token_for(api, "USR-002")

    assert (await api.get("/api/users", headers=staff)).status_code == 403


async def test_switching_a_user_off_ends_their_session(api) -> None:
    owner = await token_for(api, "USR-001")
    staff = await add_staff(api, owner)
    session = (await login(api, "kavya@example.com", PASSWORD)).json()["access_token"]

    off = await api.patch(
        f"/api/users/{staff['user_id']}", json={"is_active": False}, headers=owner
    )

    assert off.status_code == 200 and off.json()["is_active"] is False
    assert (await api.get("/api/auth/me", headers=bearer(session))).status_code == 401
    assert (await login(api, "kavya@example.com", PASSWORD)).status_code == 401


async def test_the_same_email_or_phone_cannot_belong_to_two_users(api) -> None:
    owner = await token_for(api, "USR-001")
    await add_staff(api, owner)

    twice = await api.post(
        "/api/users",
        json={"name": "Other", "email": "kavya@example.com", "password": PASSWORD},
        headers=owner,
    )
    seed_phone = await api.post(
        "/api/users", json={"name": "Other", "phone": SEED_OWNER_PHONE}, headers=owner
    )

    assert twice.status_code == seed_phone.status_code == 409


async def test_the_dev_login_still_works_after_a_password_change(api) -> None:
    await add_staff(api, await token_for(api, "USR-001"))
    token = (await login(api, "kavya@example.com", PASSWORD)).json()["access_token"]
    me = (await api.get("/api/auth/me", headers=bearer(token))).json()
    await api.post(
        "/api/auth/password",
        json={"current_password": PASSWORD, "new_password": "masala-dosa-on-sunday"},
        headers=bearer(token),
    )

    dev = await token_for(api, me["user_id"])

    assert (await api.get("/api/auth/me", headers=dev)).json()["authenticated"]
