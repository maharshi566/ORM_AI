"""The morning briefing: the planted problems appear, it is stored once a day, and the
morning run sends each owner at most one WhatsApp message."""

import pytest
from sqlalchemy import func, select

from app.models import Briefing
from app.services.briefing_service import build_briefing, todays_briefing
from tests.conftest import ANCHOR_NOON
from tests.test_api_phase6 import make_client, token_for  # noqa: F401  (fixture)

SECRET = "a-long-random-scheduler-secret"


async def test_shop_001s_planted_problems_are_in_its_briefing(session_factory) -> None:
    async with session_factory() as db:
        briefing = await build_briefing(db, "SHOP-001", ANCHOR_NOON)

    low = {p["product_id"]: p for p in briefing.low_stock}
    overdue = {c["customer_id"] for c in briefing.overdue}
    text = briefing.text()
    assert briefing.briefing_date == "2026-09-30" and briefing.owner_name == "Ramesh Naidu"
    assert "PRD-0003" in low and not low["PRD-0003"]["on_order"]  # low, nothing ordered
    assert low.get("PRD-0009", {"on_order": True})["on_order"]  # low, already on order
    assert briefing.low_stock_not_ordered < briefing.low_stock_count
    assert "CUST-0002" in overdue  # the overdue khata
    assert text.startswith("Good morning Ramesh.") and "PRD-0003" in text
    assert len(briefing.template_values()) == 6


async def test_a_late_delivery_is_named(session_factory) -> None:
    async with session_factory() as db:
        briefing = await build_briefing(db, "SHOP-002", ANCHOR_NOON)

    assert {
        "purchase_order_id": "PO-00585",
        "supplier_name": briefing.late_orders[0]["supplier_name"],
        "days_late": 7,
    } in briefing.late_orders
    assert "PO-00585" in briefing.text() and "7 days late" in briefing.text()


async def test_the_briefing_is_built_once_a_day(session_factory) -> None:
    first = await todays_briefing(session_factory, "SHOP-001", ANCHOR_NOON)
    again = await todays_briefing(session_factory, "SHOP-001", ANCHOR_NOON)

    async with session_factory() as db:
        count = await db.scalar(select(func.count()).select_from(Briefing))
    assert first.id == again.id and count == 1


# ------------------------------------------------------------------ the API


async def test_a_shop_user_sees_their_own_shops_briefing(make_client) -> None:  # noqa: F811
    api = make_client(rate_limit_enabled=False)
    staff = await token_for(api, "USR-002")

    own = await api.get("/api/briefings/today", headers=staff)
    other = await api.get("/api/briefings/today?shop_id=SHOP-002", headers=staff)

    assert own.status_code == 200 and own.json()["shop_id"] == "SHOP-001"
    assert own.json()["text"].startswith("Good morning")
    assert own.json()["sent_at"] is None
    assert other.status_code == 403


async def test_the_morning_run_needs_the_scheduler_secret_or_an_admin(make_client) -> None:  # noqa: F811
    api = make_client(rate_limit_enabled=False, auth_required=True, briefing_cron_secret=SECRET)
    owner = await token_for(api, "USR-001")

    nobody = await api.post("/api/briefings/run")
    wrong = await api.post("/api/briefings/run", headers={"X-Cron-Secret": "guess"})
    an_owner = await api.post("/api/briefings/run", headers=owner)
    scheduler = await api.post(
        "/api/briefings/run?shop_id=SHOP-001", headers={"X-Cron-Secret": SECRET}
    )

    assert nobody.status_code == wrong.status_code == 401
    assert an_owner.status_code == 403
    assert scheduler.status_code == 200
    assert scheduler.json() == {"built": 1, "sent": 0, "not_sent": 0, "failed": []}


@pytest.mark.parametrize("shops", ["SHOP-001", ""])
async def test_whatsapp_goes_only_to_listed_shops_and_only_once(make_client, shops) -> None:  # noqa: F811
    api = make_client(
        rate_limit_enabled=False,
        briefing_cron_secret=SECRET,
        messaging_provider="whatsapp",
        briefing_shops=shops,
    )
    scheduler = {"X-Cron-Secret": SECRET}
    query = "/api/briefings/run?shop_id=SHOP-001&shop_id=SHOP-002"

    first = (await api.post(query, headers=scheduler)).json()
    second = (await api.post(query, headers=scheduler)).json()

    sent = api.app.state.agent_runtime.clients["messaging_api"].sent
    if shops:  # listed: SHOP-001 gets it, once
        assert (first["sent"], first["not_sent"]) == (1, 1)
        assert [m["purpose"] for m in sent] == ["daily_briefing"]
        assert len(sent[0]["params"]) == 6 and sent[0]["params"][0] == "Ramesh"
    else:  # not listed: the synthetic owners' numbers are made up, so nobody gets it
        assert (first["sent"], first["not_sent"]) == (0, 2) and sent == []
    assert second["sent"] == 0  # already sent today, or still nobody to send to
