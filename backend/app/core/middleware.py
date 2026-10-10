"""Request context middleware: request IDs and one access-log line per request.

Written as plain ASGI middleware (not ``BaseHTTPMiddleware``) so it will not
break the Server-Sent Events stream added in Phase 6.
"""

import re
import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import get_logger

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

logger = get_logger(__name__)


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            # Reject anything that could be used for log injection.
            return candidate if _VALID_REQUEST_ID.match(candidate) else None
    return None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            logger.info(
                "request_completed",
                method=scope["method"],
                path=scope["path"],
                status=status_code,
                latency_ms=round((time.perf_counter() - start) * 1000, 2),
            )
            structlog.contextvars.clear_contextvars()


class BodySizeLimitMiddleware:
    """Refuse request bodies that are too big, before they are read into memory or disk.

    FastAPI reads an upload's whole form (spooling it to a temporary file) before the
    route, its login check or its size check run. Without this, anyone could send an
    endless upload and only then be told 401 or 413. Here a body over the limit is
    refused at once from its Content-Length header, or as soon as the bytes received
    pass the limit (for bodies sent without one).

    Uploads may be ``UPLOAD_MAX_MB`` plus room for the form fields; everything else
    (JSON) at most ``OTHER_MAX_BYTES``.
    """

    OTHER_MAX_BYTES = 1_000_000
    FORM_ROOM_BYTES = 256_000

    def __init__(self, app: ASGIApp, *, upload_max_bytes: int, upload_path: str) -> None:
        self.app = app
        self.upload_max = upload_max_bytes + self.FORM_ROOM_BYTES
        self.upload_path = upload_path

    def _limit(self, scope: Scope) -> int:
        # endswith: the path may start with a root path when served behind a proxy
        path = scope.get("path", "")
        return self.upload_max if path.endswith(self.upload_path) else self.OTHER_MAX_BYTES

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from starlette.exceptions import HTTPException

        limit = self._limit(scope)
        too_big = f"The request is larger than {limit / 1_000_000:.1f} MB."
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > limit:
                    await _send_json(send, 413, "request_too_large", too_big, scope)
                    return
        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Raised inside the app, so the normal error handler answers 413.
                    raise HTTPException(status_code=413, detail=too_big)
            return message

        await self.app(scope, counting_receive, send)


async def _send_json(send: Send, status: int, code: str, message: str, scope: Scope) -> None:
    import json

    request_id = scope.get("state", {}).get("request_id")
    body = json.dumps(
        {"error": {"code": code, "message": message, "request_id": request_id, "details": None}}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
