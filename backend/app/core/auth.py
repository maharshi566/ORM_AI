"""Who is calling: signed login tokens and the ``current_user`` dependency.

A token is a standard JWT (HS256), made and checked here with the standard library,
so no extra package is needed. It carries the user's ID, shop and role, and expires
after ``AUTH_TOKEN_HOURS``. Send it as ``Authorization: Bearer <token>``.

* ``AUTH_REQUIRED=false`` (local default): a call without a token still works, and
  the user is taken from the request body (``user_id``), as in Phases 4 and 5. A call
  *with* a token is checked and the token wins: its shop and user are used.
* ``AUTH_REQUIRED=true`` (production): every chat, approval and record call needs a
  valid token, and a user can only reach their own shop.

``POST /api/auth/dev-token`` hands out a token for any user ID, for development and the
demo. It is refused in production; a real deployment issues tokens from its login
system (any that can sign HS256 JWTs with ``AUTH_SECRET``).
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config.settings import Settings, get_settings
from app.core.exceptions import AppError

Role = Literal["owner", "staff", "admin"]
_PROCESS_SECRET = secrets.token_bytes(32)  # used only when AUTH_SECRET is not set


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


@dataclass(frozen=True)
class Principal:
    user_id: str
    shop_id: str | None
    role: Role


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _secret(settings: Settings) -> bytes:
    if settings.auth_secret and settings.auth_secret.get_secret_value().strip():
        return settings.auth_secret.get_secret_value().strip().encode()
    return _PROCESS_SECRET


def create_token(principal: Principal, settings: Settings, *, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": principal.user_id,
        "shop": principal.shop_id,
        "role": principal.role,
        "iat": issued,
        "exp": issued + settings.auth_token_hours * 3600,
    }
    signing_input = ".".join(
        _b64(json.dumps(part, separators=(",", ":")).encode()) for part in (header, payload)
    )
    signature = hmac.new(_secret(settings), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


def decode_token(token: str, settings: Settings, *, now: float | None = None) -> Principal:
    """The principal in a valid token. Raises UnauthorizedError otherwise."""
    invalid = UnauthorizedError("The login token is not valid.")
    if len(token) > 4096:
        raise invalid
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        header = json.loads(_unb64(header_b64))
        signature = _unb64(signature_b64)
    except (ValueError, json.JSONDecodeError) as exc:  # binascii.Error is a ValueError
        raise invalid from exc
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise invalid
    expected = hmac.new(
        _secret(settings), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(signature, expected):
        raise invalid
    # Signed by us, but still checked: a token from an older version could differ.
    try:
        payload: Any = json.loads(_unb64(payload_b64))
        expires = int(payload["exp"])
        user_id, role, shop_id = payload["sub"], payload["role"], payload.get("shop")
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise invalid from exc
    if expires < int(now if now is not None else time.time()):
        raise UnauthorizedError("The login token has expired. Log in again.")
    if role not in {"owner", "staff", "admin"} or not isinstance(user_id, str) or not user_id:
        raise invalid
    if shop_id is not None and not isinstance(shop_id, str):
        raise invalid
    return Principal(user_id=user_id, shop_id=shop_id, role=role)


async def _still_valid(request: Request, principal: Principal) -> Principal:
    """The user as the database has them now.

    A token is valid for AUTH_TOKEN_HOURS, but a user can be switched off, or move to
    another shop or role, before it expires. Each call therefore uses the user's
    current shop and role; a user who is gone or switched off is logged out.
    """
    from sqlalchemy.exc import InterfaceError, OperationalError

    from app.api.identity import session_factory_for
    from app.core.exceptions import DATABASE_MESSAGE, DependencyUnavailableError
    from app.models import User

    try:
        async with session_factory_for(request)() as db:
            user = await db.get(User, principal.user_id)
    except (OSError, TimeoutError, OperationalError, InterfaceError) as exc:
        # OSError includes "host not found" (socket.gaierror) and "connection refused"
        raise DependencyUnavailableError(DATABASE_MESSAGE) from exc
    if user is None or not user.is_active:
        raise UnauthorizedError("This login is no longer valid. Log in again.")
    role = str(user.role)
    if role not in {"owner", "staff", "admin"}:
        raise UnauthorizedError("This login is no longer valid. Log in again.")
    return Principal(user_id=user.id, shop_id=user.shop_id, role=role)  # type: ignore[arg-type]


# Declares "Authorization: Bearer <token>" in the API description, which gives the
# /docs page its Authorize button. auto_error=False: a missing token is decided below.
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="A login token from POST /api/auth/dev-token (development) or your login system.",
)


async def current_user(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Principal | None:
    """The caller, from the Bearer token; None when there is none and none is required."""
    token = credentials.credentials.strip() if credentials else ""
    if not token:
        if settings.auth_required:
            raise UnauthorizedError(
                "Log in first: send Authorization: Bearer <token> (POST /api/auth/dev-token "
                "gives one in development)."
            )
        return None
    principal = await _still_valid(request, decode_token(token, settings))
    request.state.user_id = principal.user_id
    return principal


def check_shop(principal: Principal | None, shop_id: str) -> None:
    """A logged-in user may only reach their own shop (admins reach every shop)."""
    from app.core.exceptions import ForbiddenError

    if principal is None or principal.role == "admin":
        return
    if principal.shop_id != shop_id:
        raise ForbiddenError(f"You are logged in for {principal.shop_id}, not {shop_id}.")


CurrentUser = Annotated[Principal | None, Depends(current_user)]
