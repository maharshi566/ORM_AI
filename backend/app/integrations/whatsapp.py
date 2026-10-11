"""WhatsApp Cloud API: Meta's official API for business messages (docs/whatsapp.md).

A business may start a conversation only with a **template** that Meta has approved,
so every message here is a template: ORM_AI fills in its variables ({{1}}, {{2}}, ...).
Which template serves which purpose is set in .env (``WHATSAPP_TEMPLATE_*``); the
texts to create in WhatsApp Manager are in docs/whatsapp.md.

``WhatsAppClient`` sends one template and returns Meta's message ID (``wamid...``),
which the delivery webhook later reports on (app/api/routes/webhooks.py). Errors
become ``WhatsAppError`` with a plain sentence and whether a retry can help; the
access token never appears in a message or a log line.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config.settings import Settings

GRAPH_URL = "https://graph.facebook.com"
PARAM_MAX_CHARS = 1000  # Meta's limit is 1024 per variable

# Meta's error codes that a retry can fix: throttling, and temporary trouble on Meta's side.
RETRYABLE_CODES = {1, 2, 4, 17, 341, 80007, 130429, 131000, 131016, 131056, 133004}
TEMPLATE_CODES = {132000, 132001, 132005, 132007, 132012, 132015, 132016}


class WhatsAppError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


def clean_param(value: object) -> str:
    """A template variable as Meta accepts it: no line breaks or tabs, no runs of more
    than four spaces, at most 1,000 characters."""
    text = re.sub(r"[\r\n\t]+", " ", str(value))
    text = re.sub(r" {2,}", " ", text).strip()
    if len(text) > PARAM_MAX_CHARS:
        text = text[: PARAM_MAX_CHARS - 1].rstrip() + "…"
    return text or "-"


@dataclass(frozen=True)
class Template:
    name: str
    language: str
    body: Sequence[str] = field(default_factory=tuple)  # values for {{1}}, {{2}}, ...
    # Authentication templates: the code again, for the "Copy code" button.
    copy_code: str | None = None

    def components(self) -> list[dict[str, Any]]:
        components: list[dict[str, Any]] = []
        if self.body:
            parameters = [{"type": "text", "text": clean_param(v)} for v in self.body]
            components.append({"type": "body", "parameters": parameters})
        if self.copy_code is not None:
            components.append(
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [{"type": "text", "text": self.copy_code}],
                }
            )
        return components


def _explain(status: int, payload: Any, template: str) -> WhatsAppError:
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    code = error.get("code")
    details = (error.get("error_data") or {}).get("details") or error.get("message") or ""
    detail = f" ({details})" if details else ""
    if status == 401 or code == 190:
        return WhatsAppError(
            "WhatsApp rejected the access token (WHATSAPP_ACCESS_TOKEN); it may have expired. "
            "Create a permanent System User token in Meta Business Settings.",
            retryable=False,
            code=code,
        )
    if code in TEMPLATE_CODES:
        return WhatsAppError(
            f"WhatsApp refused the template '{template}'{detail}. Check that it exists, is "
            "approved, and has the variables docs/whatsapp.md lists.",
            retryable=False,
            code=code,
        )
    if code == 131030:
        return WhatsAppError(
            "This number is not on the test list: while the WhatsApp app is in development "
            "mode, Meta only delivers to numbers added under API Setup.",
            retryable=False,
            code=code,
        )
    if code == 131026:
        return WhatsAppError(
            "The message could not be delivered: the number may not be on WhatsApp.",
            retryable=False,
            code=code,
        )
    if status == 429 or code in RETRYABLE_CODES or status >= 500:
        return WhatsAppError(
            f"WhatsApp is busy or rate-limiting (HTTP {status}){detail}; try again shortly.",
            retryable=True,
            code=code,
        )
    return WhatsAppError(
        f"WhatsApp refused the message (HTTP {status}){detail}.", retryable=False, code=code
    )


class WhatsAppClient:
    def __init__(
        self, settings: Settings, *, http: httpx.AsyncClient | None = None, timeout: float = 10.0
    ) -> None:
        token = settings.whatsapp_access_token
        self._token = token.get_secret_value().strip() if token else ""
        self._url = (
            f"{GRAPH_URL}/{settings.whatsapp_api_version.strip()}/"
            f"{settings.whatsapp_phone_number_id.strip()}/messages"
        )
        self._http = http
        self._timeout = timeout

    async def send_template(self, to_e164: str, template: Template) -> str:
        """Send one template message; returns Meta's message ID (wamid...)."""
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_e164.removeprefix("+"),
            "type": "template",
            "template": {
                "name": template.name,
                "language": {"code": template.language},
                "components": template.components(),
            },
        }
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            if self._http is not None:
                response = await self._http.post(self._url, json=body, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as http:
                    response = await http.post(self._url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise WhatsAppError("WhatsApp did not answer in time.", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise WhatsAppError(
                f"Could not reach WhatsApp ({type(exc).__name__}).", retryable=True
            ) from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            raise _explain(response.status_code, payload, template.name)
        try:
            return str(payload["messages"][0]["id"])
        except (KeyError, IndexError, TypeError) as exc:
            raise WhatsAppError(
                "WhatsApp accepted the message but returned no message ID.", retryable=False
            ) from exc
