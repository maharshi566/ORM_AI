"""The morning briefing (docs/briefing.md).

* ``GET /api/briefings/today?shop_id=SHOP-001``: today's briefing for a shop, built on
  the first request of the day. Shop users get their own shop.
* ``POST /api/briefings/run``: build every shop's briefing and, with WhatsApp set up,
  send it to each owner. Called each morning by a scheduler with the
  ``X-Cron-Secret`` header (``BRIEFING_CRON_SECRET``), or by an admin. Running it twice
  sends nothing twice.
"""

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request

from app.api.identity import session_factory_for
from app.config.settings import Settings, get_settings
from app.core.auth import CurrentUser, OptionalUser, UnauthorizedError
from app.core.exceptions import ForbiddenError, InvalidRequestError
from app.models.schemas import BriefingRunResponse, BriefingView
from app.services.briefing_service import run_briefings, todays_briefing
from app.services.records_service import scope_shop
from app.tools.api_tools import default_clients
from app.tools.base import business_now

router = APIRouter(prefix="/briefings", tags=["briefings"])


def _messaging(request: Request, settings: Settings) -> Any:
    """The WhatsApp client when messages really go out; None otherwise (nothing is sent)."""
    if settings.messaging_provider != "whatsapp":
        return None
    runtime = getattr(request.app.state, "agent_runtime", None)
    if runtime is not None and "messaging_api" in runtime.clients:
        return runtime.clients["messaging_api"]
    return default_clients(settings)["messaging_api"]


@router.get("/today", response_model=BriefingView, summary="Today's briefing for a shop")
async def today(
    request: Request,
    principal: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
    shop_id: Annotated[str | None, Query(max_length=16)] = None,
) -> BriefingView:
    shop = scope_shop(principal, shop_id)
    if shop is None:
        raise InvalidRequestError("Say which shop (shop_id).")
    row = await todays_briefing(
        session_factory_for(request), shop, business_now(settings.business_date)
    )
    return BriefingView(
        shop_id=row.shop_id,
        briefing_date=row.briefing_date,
        text=row.text,
        content=row.content,
        sent_at=row.sent_at,
        sent_to=row.sent_to,
    )


@router.post("/run", response_model=BriefingRunResponse, summary="Build and send the briefings")
async def run(
    request: Request,
    principal: OptionalUser,
    settings: Annotated[Settings, Depends(get_settings)],
    x_cron_secret: Annotated[str | None, Header()] = None,
    shop_id: Annotated[list[str] | None, Query(max_length=16)] = None,
) -> BriefingRunResponse:
    secret = settings.briefing_cron_secret
    expected = secret.get_secret_value().strip() if secret else ""
    by_scheduler = bool(expected and x_cron_secret) and hmac.compare_digest(
        (x_cron_secret or "").strip(), expected
    )
    if not by_scheduler:
        if x_cron_secret:
            raise UnauthorizedError("The X-Cron-Secret header does not match BRIEFING_CRON_SECRET.")
        if principal is not None and principal.role != "admin":
            raise ForbiddenError("Only an admin or the scheduler runs every shop's briefing.")
        if principal is None and settings.auth_required:
            raise UnauthorizedError("Send X-Cron-Secret, or log in as an admin.")
    result = await run_briefings(
        session_factory_for(request),
        settings,
        _messaging(request, settings),
        business_now(settings.business_date),
        shop_ids=[s.upper() for s in shop_id] if shop_id else None,
    )
    return BriefingRunResponse(
        built=result.built, sent=result.sent, not_sent=result.not_sent, failed=result.failed
    )
