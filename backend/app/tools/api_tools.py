"""External APIs: the mocks (with failure injection), real WhatsApp, and the tool
that calls the supplier API.

A real shop connects to a supplier's ordering system and to WhatsApp. The supplier
API is simulated here; messaging is simulated too (``MESSAGING_PROVIDER=mock``), or
goes out through the WhatsApp Cloud API (``MESSAGING_PROVIDER=whatsapp``,
``WhatsAppMessagingAPI``). Every mock call can be made to fail on purpose
(``MOCK_API_FAILURE_MODE``), so the agents' error handling and the tool-failure
evaluation cases can be tested without real services.
"""

import asyncio
import hashlib
import random
from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from pydantic import Field

from app.config.settings import Settings
from app.core.phones import is_dialable, normalize_phone
from app.integrations.whatsapp import Template, WhatsAppClient, WhatsAppError
from app.tools.base import ToolContext, ToolError, ToolErrorCode, ToolInput, ToolOutput, ToolSpec
from app.tools.helpers import get_product, get_supplier_row, margin_percent, money


class FailureMode(StrEnum):
    NONE = "none"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    RANDOM = "random"


class UpstreamError(ToolError):
    """An external API failed. Retryable unless it is a 'not found'."""


class FaultInjector:
    """Decides, call by call, whether a mock API succeeds, fails or hangs.

    ``fail_first`` makes only the first N calls fail, which is how the tests
    check that read tools recover with a retry.
    """

    def __init__(
        self,
        mode: FailureMode | str = FailureMode.NONE,
        *,
        latency_ms: int = 0,
        random_rate: float = 0.2,
        fail_first: int | None = None,
        seed: int | None = None,
    ) -> None:
        self.mode = FailureMode(mode)
        self.latency_ms = latency_ms
        self.random_rate = random_rate
        self.fail_first = fail_first
        self.calls = 0
        self._rng = random.Random(seed)  # noqa: S311 - simulation only

    @classmethod
    def from_settings(cls, settings: Settings) -> "FaultInjector":
        return cls(settings.mock_api_failure_mode, latency_ms=settings.mock_api_latency_ms)

    async def before_call(self, api: str) -> None:
        self.calls += 1
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)
        mode = self.mode
        if self.fail_first is not None and self.calls > self.fail_first:
            mode = FailureMode.NONE
        if mode == FailureMode.RANDOM:
            mode = (
                self._rng.choice([FailureMode.SERVER_ERROR, FailureMode.TIMEOUT])
                if self._rng.random() < self.random_rate
                else FailureMode.NONE
            )
        if mode == FailureMode.TIMEOUT:
            await asyncio.sleep(3600)  # the tool's timeout cancels this
        if mode == FailureMode.SERVER_ERROR:
            raise UpstreamError(ToolErrorCode.UPSTREAM_ERROR, f"{api} returned HTTP 500")
        if mode == FailureMode.RATE_LIMITED:
            raise UpstreamError(ToolErrorCode.RATE_LIMITED, f"{api} returned HTTP 429")
        if mode == FailureMode.NOT_FOUND:
            raise UpstreamError(ToolErrorCode.NOT_FOUND, f"{api} returned HTTP 404")


def _short_hash(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:10].upper()


class MockSupplierAPI:
    """Stands in for a supplier's ordering system."""

    name = "supplier_api"

    def __init__(self, faults: FaultInjector | None = None) -> None:
        self.faults = faults or FaultInjector()

    async def get_quote(self, supplier_id: str, sku: str, recorded_cost: Decimal) -> dict[str, Any]:
        await self.faults.before_call(self.name)
        # The mock simply confirms the rate on record; a real API would return its own.
        return {"supplier_id": supplier_id, "sku": sku, "unit_cost": recorded_cost}

    async def submit_order(self, purchase_order_id: str, supplier_id: str) -> dict[str, Any]:
        await self.faults.before_call(self.name)
        return {"supplier_ref": f"{supplier_id}-{_short_hash(purchase_order_id)}", "accepted": True}


class MessagingAPI(Protocol):
    """Sends one message. ``text`` is the full message (kept in ``notifications``);
    ``purpose`` and ``params`` pick and fill the approved WhatsApp template."""

    name: str

    async def send(
        self,
        channel: str,
        recipient: str,
        text: str,
        *,
        purpose: str = "",
        params: Sequence[str] = (),
    ) -> dict[str, str]: ...


class MockMessagingAPI:
    """Stands in for WhatsApp: nothing leaves the backend."""

    name = "messaging_api"

    def __init__(self, faults: FaultInjector | None = None) -> None:
        self.faults = faults or FaultInjector()
        self.sent: list[dict[str, Any]] = []  # handy for tests

    async def send(
        self,
        channel: str,
        recipient: str,
        text: str,
        *,
        purpose: str = "",
        params: Sequence[str] = (),
    ) -> dict[str, str]:
        await self.faults.before_call(self.name)
        message = {
            "message_id": f"{channel[:2].upper()}-{_short_hash(recipient, text)}",
            "channel": channel,
            "recipient": recipient,
            "text": text,
        }
        self.sent.append({**message, "purpose": purpose, "params": list(params)})
        return message


class WhatsAppMessagingAPI:
    """Sends through the WhatsApp Cloud API, as the template for each purpose.

    Safety first: a number that cannot be a real one (the synthetic data's) is refused,
    and ``WHATSAPP_TEST_RECIPIENT`` sends every message to one test phone instead.
    """

    name = "messaging_api"

    def __init__(self, settings: Settings, client: WhatsAppClient | None = None) -> None:
        self.settings = settings
        self.client = client or WhatsAppClient(settings)
        self.templates = {
            "payment_reminder": settings.whatsapp_template_payment_reminder,
            "supplier_follow_up": settings.whatsapp_template_supplier_follow_up,
            "daily_briefing": settings.whatsapp_template_daily_briefing,
        }

    def recipient_for(self, recipient: str) -> str:
        """The E.164 number the message really goes to."""
        test = self.settings.whatsapp_test_recipient.strip()
        target = normalize_phone(test) if test else normalize_phone(recipient)
        if not is_dialable(target):
            where = "WHATSAPP_TEST_RECIPIENT" if test else "The number on record"
            raise UpstreamError(
                ToolErrorCode.INVALID_INPUT,
                f"{where} ({test or recipient}) cannot receive WhatsApp messages; "
                "nothing was sent.",
            )
        return target  # type: ignore[return-value]

    async def send(
        self,
        channel: str,
        recipient: str,
        text: str,
        *,
        purpose: str = "",
        params: Sequence[str] = (),
    ) -> dict[str, str]:
        if channel != "whatsapp":
            raise UpstreamError(
                ToolErrorCode.INVALID_INPUT, "Only WhatsApp is connected; SMS is not set up."
            )
        template_name = self.templates.get(purpose)
        if not template_name:
            raise UpstreamError(
                ToolErrorCode.INVALID_INPUT, f"No WhatsApp template is set for '{purpose}'."
            )
        target = self.recipient_for(recipient)
        template = Template(template_name, self.settings.whatsapp_language, tuple(params))
        try:
            message_id = await self.client.send_template(target, template)
        except WhatsAppError as exc:
            code = (
                ToolErrorCode.RATE_LIMITED
                if exc.code in {4, 80007, 130429}
                else (
                    ToolErrorCode.UPSTREAM_ERROR if exc.retryable else ToolErrorCode.INVALID_INPUT
                )
            )
            raise UpstreamError(code, str(exc)) from exc
        return {
            "message_id": message_id,
            "channel": channel,
            "recipient": recipient,
            "text": text,
            "delivered_to": target,
        }


def default_clients(settings: Settings) -> dict[str, Any]:
    faults = FaultInjector.from_settings(settings)
    messaging: MessagingAPI = (
        WhatsAppMessagingAPI(settings)
        if settings.messaging_provider == "whatsapp"
        else MockMessagingAPI(faults)
    )
    return {"supplier_api": MockSupplierAPI(faults), "messaging_api": messaging}


# --------------------------------------------------------------------- tool


class CheckSupplierPriceInput(ToolInput):
    product_id: str = Field(description="Product ID, e.g. PRD-0085")


class CheckSupplierPriceOutput(ToolOutput):
    product_id: str
    product_name: str
    supplier_id: str
    supplier_name: str
    quoted_unit_cost: Decimal
    recorded_cost_price: Decimal
    selling_price: Decimal
    mrp: Decimal
    margin_percent_at_quote: float = Field(description="Selling price vs quoted cost, in percent")


async def check_supplier_price(
    ctx: ToolContext, args: CheckSupplierPriceInput
) -> CheckSupplierPriceOutput:
    product = await get_product(ctx, args.product_id)
    if not product.preferred_supplier_id:
        raise ToolError(ToolErrorCode.NOT_FOUND, f"{product.id} has no preferred supplier.")
    supplier = await get_supplier_row(ctx, product.preferred_supplier_id)
    api: MockSupplierAPI = ctx.clients["supplier_api"]
    quote = await api.get_quote(supplier.id, product.sku, product.cost_price)
    cost = money(quote["unit_cost"])
    return CheckSupplierPriceOutput(
        product_id=product.id,
        product_name=product.name,
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        quoted_unit_cost=cost,
        recorded_cost_price=money(product.cost_price),
        selling_price=money(product.selling_price),
        mrp=money(product.mrp),
        margin_percent_at_quote=margin_percent(product.selling_price, cost),
    )


API_TOOLS = [
    ToolSpec(
        name="check_supplier_price",
        description=(
            "Ask the product's preferred supplier for its current unit cost, and compare it "
            "with the recorded cost, selling price and MRP. Use when a margin looks wrong."
        ),
        input_model=CheckSupplierPriceInput,
        output_model=CheckSupplierPriceOutput,
        handler=check_supplier_price,
        kind="read",
        timeout_seconds=3.0,
        max_retries=2,
    ),
]
