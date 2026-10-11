"""WhatsApp Cloud API: the request Meta receives, its errors, the safety rules, the
reminder tool through it, login codes, and delivery receipts."""

import hashlib
import hmac
import json

import httpx
import pytest
from sqlalchemy import select

from app.config.settings import Settings
from app.integrations.whatsapp import Template, WhatsAppClient, WhatsAppError, clean_param
from app.models import Notification
from app.services.auth_service import WhatsAppCodeSender
from app.tools.api_tools import UpstreamError, WhatsAppMessagingAPI
from app.tools.base import ToolErrorCode
from tests.test_api_phase6 import make_client  # noqa: F401  (fixture)
from tests.test_tools_actions import _remindable_customer, act

TOKEN = "EAAG-very-secret-token"
APP_SECRET = "meta-app-secret"
VERIFY = "my-verify-token"
WAMID = "wamid.HBgMOTE5ODc2NTQzMjEwFQIAERgSQTFCMkMzRDRFNUY2RzdIOEkwAA=="


def settings(**values) -> Settings:
    base = {
        "_env_file": None,
        "messaging_provider": "whatsapp",
        "whatsapp_access_token": TOKEN,
        "whatsapp_phone_number_id": "123456789012345",
        "whatsapp_api_version": "v23.0",
    }
    return Settings(**{**base, **values})


class Meta:
    """A stand-in for graph.facebook.com that records what it receives."""

    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status = status
        self.payload = payload if payload is not None else {"messages": [{"id": WAMID}]}
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.status, json=self.payload)

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)


# ------------------------------------------------------------------ the client


async def test_a_template_message_is_sent_the_way_meta_expects() -> None:
    meta = Meta()
    client = WhatsAppClient(settings(), http=meta.client())

    message_id = await client.send_template(
        "+919876543210", Template("payment_reminder", "en", ("Asha", "Rs 694\nnow"))
    )

    request = meta.requests[0]
    assert message_id == WAMID
    assert str(request.url) == "https://graph.facebook.com/v23.0/123456789012345/messages"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert meta.body["to"] == "919876543210"  # no plus sign
    assert meta.body["template"] == {
        "name": "payment_reminder",
        "language": {"code": "en"},
        "components": [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": "Asha"},
                    {"type": "text", "text": "Rs 694 now"},  # no line breaks allowed
                ],
            }
        ],
    }


def test_template_values_are_cleaned_to_meta_rules() -> None:
    assert clean_param("a\n\tb     c") == "a b c"
    assert clean_param("") == "-"
    assert len(clean_param("x" * 5000)) == 1000


@pytest.mark.parametrize(
    ("status", "error", "phrase", "retryable"),
    [
        (401, {"code": 190, "message": "Session expired"}, "access token", False),
        (400, {"code": 132001, "message": "Template name does not exist"}, "template", False),
        (400, {"code": 131030, "message": "Recipient not in allowed list"}, "test list", False),
        (429, {"code": 130429, "message": "Rate limit hit"}, "rate-limiting", True),
        (503, {"code": 131000, "message": "Something went wrong"}, "busy", True),
    ],
)
async def test_meta_errors_become_plain_sentences(status, error, phrase, retryable) -> None:
    client = WhatsAppClient(settings(), http=Meta(status, {"error": error}).client())

    with pytest.raises(WhatsAppError) as caught:
        await client.send_template("+919876543210", Template("t", "en", ("x",)))

    assert phrase in str(caught.value) and caught.value.retryable is retryable
    assert TOKEN not in str(caught.value)


async def test_a_timeout_can_be_retried() -> None:
    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(hang))

    with pytest.raises(WhatsAppError) as caught:
        await WhatsAppClient(settings(), http=http).send_template(
            "+919876543210", Template("t", "en")
        )

    assert caught.value.retryable


# ------------------------------------------------------------------ safety


async def test_a_made_up_number_is_never_messaged() -> None:
    meta = Meta()
    api = WhatsAppMessagingAPI(settings(), WhatsAppClient(settings(), http=meta.client()))

    with pytest.raises(UpstreamError) as caught:
        await api.send("whatsapp", "+91-00000-30012", "Hello", purpose="payment_reminder")

    assert caught.value.code == ToolErrorCode.INVALID_INPUT and "nothing was sent" in str(
        caught.value
    )
    assert meta.requests == []


async def test_a_test_recipient_receives_everything_instead() -> None:
    config = settings(whatsapp_test_recipient="98765 43210")
    meta = Meta()
    api = WhatsAppMessagingAPI(config, WhatsAppClient(config, http=meta.client()))

    sent = await api.send(
        "whatsapp", "+91-00000-30012", "Hello", purpose="payment_reminder", params=("A", "B")
    )

    assert meta.body["to"] == "919876543210"
    assert sent["delivered_to"] == "+919876543210" and sent["recipient"] == "+91-00000-30012"


async def test_sms_and_unknown_purposes_are_refused() -> None:
    api = WhatsAppMessagingAPI(settings(), WhatsAppClient(settings(), http=Meta().client()))

    with pytest.raises(UpstreamError, match="SMS"):
        await api.send("sms", "+919876543210", "Hi", purpose="payment_reminder")
    with pytest.raises(UpstreamError, match="No WhatsApp template"):
        await api.send("whatsapp", "+919876543210", "Hi", purpose="birthday_wishes")


# ------------------------------------------------------------------ the reminder tool


async def test_a_payment_reminder_goes_out_as_the_approved_template(
    registry, make_ctx, seed_data, session_factory
) -> None:
    config = settings(whatsapp_test_recipient="+919876543210")
    meta = Meta()
    shop_id, customer_id, when = await _remindable_customer(registry, make_ctx, seed_data)
    ctx = make_ctx(shop_id, now=when)
    ctx.clients["messaging_api"] = WhatsAppMessagingAPI(
        config, WhatsAppClient(config, http=meta.client())
    )

    result = await act(
        registry,
        ctx,
        "send_payment_reminder",
        customer_id=customer_id,
        idempotency_key="t:whatsapp:reminder",
    )

    assert result.ok, result
    template = meta.body["template"]
    values = [p["text"] for p in template["components"][0]["parameters"]]
    assert template["name"] == "payment_reminder" and len(values) == 4
    assert values[0] in result.data["message"] and values[2] in result.data["message"]
    async with session_factory() as db:
        saved = await db.scalar(
            select(Notification).where(Notification.idempotency_key == "t:whatsapp:reminder")
        )
    assert saved.external_id == WAMID and str(saved.status) == "sent"


# ------------------------------------------------------------------ login codes


async def test_a_login_code_uses_the_authentication_template() -> None:
    meta = Meta()
    sender = WhatsAppCodeSender(settings(), WhatsAppClient(settings(), http=meta.client()))

    await sender.send_code("+919876543210", "482913")

    template = meta.body["template"]
    assert template["name"] == "login_code"
    assert template["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": "482913"}]},
        {
            "type": "button",
            "sub_type": "url",
            "index": "0",
            "parameters": [{"type": "text", "text": "482913"}],
        },
    ]


# ------------------------------------------------------------------ delivery receipts


def receipt(message_id: str, status: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"statuses": [{"id": message_id, "status": status}]}}]}],
    }


def signed(payload: dict, secret: str = APP_SECRET) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": f"sha256={signature}", "Content-Type": "application/json"}


@pytest.fixture
async def receipts_api(make_client, session_factory):  # noqa: F811
    api = make_client(whatsapp_app_secret=APP_SECRET, whatsapp_verify_token=VERIFY)
    async with session_factory() as db:
        row = await db.scalar(select(Notification).limit(1))
        row.external_id, row.status = WAMID, "sent"
        await db.commit()
        notification_id = row.id
    return api, notification_id


async def status_of(session_factory, notification_id: int) -> str:
    async with session_factory() as db:
        return str((await db.get(Notification, notification_id)).status)


async def test_meta_can_register_the_webhook_only_with_the_verify_token(receipts_api) -> None:
    api, _ = receipts_api
    query = {"hub.mode": "subscribe", "hub.challenge": "1158201444"}

    ok = await api.get("/api/webhooks/whatsapp", params={**query, "hub.verify_token": VERIFY})
    wrong = await api.get("/api/webhooks/whatsapp", params={**query, "hub.verify_token": "guess"})

    assert ok.status_code == 200 and ok.text == "1158201444"
    assert wrong.status_code == 403


async def test_receipts_move_a_message_forward_and_never_back(
    receipts_api, session_factory
) -> None:
    api, notification_id = receipts_api

    for status in ("delivered", "read", "delivered"):  # a late "delivered" after "read"
        body, headers = signed(receipt(WAMID, status))
        response = await api.post("/api/webhooks/whatsapp", content=body, headers=headers)
        assert response.status_code == 200

    assert await status_of(session_factory, notification_id) == "read"


async def test_a_failed_message_is_recorded(receipts_api, session_factory) -> None:
    api, notification_id = receipts_api
    body, headers = signed(receipt(WAMID, "failed"))

    await api.post("/api/webhooks/whatsapp", content=body, headers=headers)

    assert await status_of(session_factory, notification_id) == "failed"


async def test_an_unsigned_receipt_changes_nothing(receipts_api, session_factory) -> None:
    api, notification_id = receipts_api
    body, headers = signed(receipt(WAMID, "read"), secret="someone-else")

    response = await api.post("/api/webhooks/whatsapp", content=body, headers=headers)

    assert response.status_code == 403
    assert await status_of(session_factory, notification_id) == "sent"
