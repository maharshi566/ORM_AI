"""WhatsApp delivery receipts (docs/whatsapp.md, "Delivery receipts").

Meta calls this webhook when a message is sent, delivered, read or fails, and ORM_AI
updates the matching row in ``notifications`` (by Meta's message ID).

* ``GET /api/webhooks/whatsapp``: the one-time check Meta makes when you register the
  webhook; it must echo ``hub.challenge`` when ``hub.verify_token`` matches
  ``WHATSAPP_VERIFY_TOKEN``.
* ``POST /api/webhooks/whatsapp``: the receipts. Each call is signed with your app's
  secret (``X-Hub-Signature-256``); an unsigned or wrongly signed call changes nothing.

A status never goes backwards (a late "delivered" does not undo "read"), and a call
Meta sends twice changes nothing the second time. Anything that is not a receipt
(incoming customer messages) is acknowledged and ignored.
"""

import hashlib
import hmac
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from app.api.identity import session_factory_for
from app.config.settings import Settings, get_settings
from app.core.exceptions import ForbiddenError
from app.core.logging import get_logger
from app.models import Notification

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = get_logger(__name__)

RANK = {"queued": 0, "sent": 1, "delivered": 2, "read": 3}


def _secret(value: Any) -> str:
    return value.get_secret_value().strip() if value else ""


@router.get("/whatsapp", response_class=PlainTextResponse, summary="Meta's webhook check")
async def verify(
    settings: Annotated[Settings, Depends(get_settings)],
    mode: Annotated[str, Query(alias="hub.mode")] = "",
    token: Annotated[str, Query(alias="hub.verify_token")] = "",
    challenge: Annotated[str, Query(alias="hub.challenge", max_length=200)] = "",
) -> str:
    expected = _secret(settings.whatsapp_verify_token)
    if mode != "subscribe" or not expected or not hmac.compare_digest(token, expected):
        raise ForbiddenError("The verify token does not match WHATSAPP_VERIFY_TOKEN.")
    return challenge


def signature_ok(body: bytes, header: str | None, app_secret: str) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


def statuses_in(payload: Any) -> list[dict[str, Any]]:
    found = []
    for entry in payload.get("entry") or [] if isinstance(payload, dict) else []:
        for change in entry.get("changes") or []:
            found.extend((change.get("value") or {}).get("statuses") or [])
    return [s for s in found if isinstance(s, dict) and s.get("id") and s.get("status")]


@router.post("/whatsapp", summary="WhatsApp delivery receipts")
async def receipts(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, int]:
    body = await request.body()
    if not signature_ok(
        body, request.headers.get("x-hub-signature-256"), _secret(settings.whatsapp_app_secret)
    ):
        raise ForbiddenError("The webhook signature does not match WHATSAPP_APP_SECRET.")
    try:
        payload = json.loads(body)
    except ValueError:
        logger.warning("whatsapp webhook: not JSON")
        return {"updated": 0}
    updated = 0
    async with session_factory_for(request)() as db:
        for status in statuses_in(payload):
            new = str(status["status"])
            row = await db.scalar(
                select(Notification).where(Notification.external_id == str(status["id"]))
            )
            if row is None:
                continue
            old = str(row.status)
            if new == "failed":
                if old == "failed":
                    continue
                errors = status.get("errors") or [{}]
                logger.warning(
                    "whatsapp message failed",
                    notification_id=row.id,
                    code=errors[0].get("code"),
                    title=errors[0].get("title"),
                )
            elif new not in RANK or RANK[new] <= RANK.get(old, -1) or old == "failed":
                continue
            row.status = new
            updated += 1
        await db.commit()
    return {"updated": updated}
