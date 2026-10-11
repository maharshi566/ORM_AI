"""Logins and tokens (Phase 6; real logins after Phase 9).

Real logins (app/services/auth_service.py), on every server:

* ``POST /api/auth/login``: email and password.
* ``POST /api/auth/otp/request``, then ``POST /api/auth/otp/verify``: a six-digit code
  sent to the user's phone over WhatsApp (``OTP_CHANNEL``).
* ``POST /api/auth/password``: change your own password (other sessions end).
* ``GET /api/auth/methods``: which of these this server offers, for the login page.

Each answers with the same token. Send it as ``Authorization: Bearer <token>``. In
http://localhost:8000/docs, click **Authorize** (top right) and paste it.

The development login, ``POST /api/auth/dev-token``, gives a token for any user, so the
API and the frontend can be tried without passwords. It works only when ``APP_ENV`` is
``local`` or ``test`` and ``AUTH_DEV_LOGIN=true``. ``GET /api/auth/dev-users`` lists
the users it accepts (the login page shows them).

A **public demo** (Phase 9) may open the development login on any server for a short
list of demo accounts: ``AUTH_DEMO_USERS=USR-001,USR-003``. Only those users can log in
that way, and never an admin (an admin token reads and decides for every shop).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.api.identity import session_factory_for
from app.config.settings import Settings, get_settings
from app.core.auth import CurrentUser, Principal, UnauthorizedError, create_token
from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.monitoring import LOGINS
from app.core.rate_limit import login_rate_limit
from app.models import Shop, User
from app.models.schemas import (
    CodeRequestBody,
    CodeRequestResponse,
    CodeVerifyBody,
    DevTokenRequest,
    DevUserView,
    LoginMethodsResponse,
    MeResponse,
    PasswordChangeRequest,
    PasswordLoginRequest,
    TokenResponse,
)
from app.models.types import utcnow
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


# Only on a developer's machine and in tests: on any shared server (staging,
# production) anyone could otherwise log in as anyone, the admin included.
DEV_LOGIN_ENVIRONMENTS = {"local", "test"}


def _dev_login_open(settings: Settings) -> bool:
    return settings.app_env in DEV_LOGIN_ENVIRONMENTS and settings.auth_dev_login


def _demo_only(settings: Settings) -> bool:
    """False: any user may log in (development). True: only the demo accounts may.

    Refuses when neither applies, the normal state of a shared server.
    """
    if _dev_login_open(settings):
        return False
    if settings.demo_user_list:
        return True
    raise ForbiddenError(
        "Development logins are switched off on this server (only APP_ENV=local or "
        "test, with AUTH_DEV_LOGIN=true; a public demo lists its accounts in "
        "AUTH_DEMO_USERS)."
    )


def _check_demo_user(user: User, settings: Settings) -> None:
    if user.id not in settings.demo_user_list:
        raise ForbiddenError("Only the demo accounts can log in here.")
    if str(user.role) == "admin":
        raise ForbiddenError("An admin account cannot use the demo login.")


async def _names(request: Request, user_id: str) -> tuple[User | None, str | None]:
    """The user row and their shop's name."""
    async with session_factory_for(request)() as db:
        user = await db.get(User, user_id)
        shop = await db.get(Shop, user.shop_id) if user and user.shop_id else None
    return user, shop.name if shop else None


async def _token_for(request: Request, user: User, settings: Settings) -> TokenResponse:
    _, shop_name = await _names(request, user.id)
    principal = Principal(
        user_id=user.id,
        shop_id=user.shop_id,
        role=str(user.role),  # type: ignore[arg-type]
        token_version=user.token_version or 0,
    )
    return TokenResponse(
        access_token=create_token(principal, settings),
        expires_in=settings.auth_token_hours * 3600,
        user_id=user.id,
        shop_id=user.shop_id,
        role=str(user.role),
        name=user.name,
        shop_name=shop_name,
    )


def _code_sender(request: Request, settings: Settings) -> auth_service.CodeSender:
    """The app's code sender, made on first use (tests put their own on app.state)."""
    sender = getattr(request.app.state, "code_sender", None)
    if sender is None:
        sender = auth_service.code_sender_for(settings)
        if sender is None:
            raise ForbiddenError("Login codes are switched off on this server (OTP_CHANNEL).")
        request.app.state.code_sender = sender
    return sender


# ------------------------------------------------------------------ real logins


@router.get("/methods", response_model=LoginMethodsResponse, summary="Login methods offered")
async def methods(settings: Annotated[Settings, Depends(get_settings)]) -> LoginMethodsResponse:
    mode = settings.otp_mode
    return LoginMethodsResponse(
        password=settings.auth_password_login,
        otp=mode != "off",
        otp_channel=None if mode == "off" else mode,
        demo=_dev_login_open(settings) or bool(settings.demo_user_list),
    )


@router.post("/login", response_model=TokenResponse, summary="Log in with email and password")
async def login(
    request: Request,
    body: PasswordLoginRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(login_rate_limit)],
) -> TokenResponse:
    if not settings.auth_password_login:
        raise ForbiddenError("Password logins are switched off on this server.")
    try:
        async with session_factory_for(request)() as db:
            user = await auth_service.password_login(db, body.email, body.password, utcnow())
    except auth_service.LockedError:
        LOGINS.labels("password", "locked").inc()
        raise
    except UnauthorizedError:
        LOGINS.labels("password", "failed").inc()
        raise
    LOGINS.labels("password", "success").inc()
    return await _token_for(request, user, settings)


@router.post(
    "/otp/request",
    response_model=CodeRequestResponse,
    status_code=202,
    summary="Send a one-time login code to a phone",
)
async def otp_request(
    request: Request,
    body: CodeRequestBody,
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(login_rate_limit)],
) -> CodeRequestResponse:
    sender = _code_sender(request, settings)
    async with session_factory_for(request)() as db:
        answer = await auth_service.request_login_code(db, body.phone, sender, settings, utcnow())
    where = "on WhatsApp" if answer.channel == "whatsapp" else "in the backend log"
    return CodeRequestResponse(
        message=f"If this number has an account, a code is on its way {where}.",
        channel=answer.channel,
        expires_in=answer.expires_in,
        resend_after=answer.resend_after,
    )


@router.post("/otp/verify", response_model=TokenResponse, summary="Log in with the code")
async def otp_verify(
    request: Request,
    body: CodeVerifyBody,
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(login_rate_limit)],
) -> TokenResponse:
    if settings.otp_mode == "off":
        raise ForbiddenError("Login codes are switched off on this server (OTP_CHANNEL).")
    try:
        async with session_factory_for(request)() as db:
            user = await auth_service.verify_login_code(
                db, body.phone, body.code.strip(), settings, utcnow()
            )
    except UnauthorizedError:
        LOGINS.labels("code", "failed").inc()
        raise
    LOGINS.labels("code", "success").inc()
    return await _token_for(request, user, settings)


@router.post(
    "/password",
    response_model=TokenResponse,
    summary="Change your password (your other sessions end)",
)
async def change_password(
    request: Request,
    body: PasswordChangeRequest,
    principal: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(login_rate_limit)],
) -> TokenResponse:
    if principal is None:
        raise UnauthorizedError("Log in first.")
    async with session_factory_for(request)() as db:
        user = await auth_service.change_password(
            db, principal.user_id, body.current_password, body.new_password, utcnow()
        )
    return await _token_for(request, user, settings)  # a fresh token for this session


# ------------------------------------------------------------------ development login


@router.post("/dev-token", response_model=TokenResponse, summary="A login token for a demo user")
async def dev_token(
    request: Request,
    body: DevTokenRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(login_rate_limit)],
) -> TokenResponse:
    demo_only = _demo_only(settings)
    user, _shop_name = await _names(request, body.user_id)
    if user is None or not user.is_active:
        raise NotFoundError(f"No active user {body.user_id}.")
    if demo_only:
        _check_demo_user(user, settings)
    return await _token_for(request, user, settings)


@router.get(
    "/dev-users",
    response_model=list[DevUserView],
    summary="The demo users the development login accepts",
)
async def dev_users(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(login_rate_limit)],
) -> list[DevUserView]:
    demo_only = _demo_only(settings)
    query = (
        select(User, Shop.name)
        .join(Shop, Shop.id == User.shop_id, isouter=True)
        .where(User.is_active.is_(True))
        .order_by(User.shop_id.is_(None), User.shop_id, User.id)
    )
    if demo_only:
        query = query.where(User.id.in_(settings.demo_user_list), User.role != "admin")
    async with session_factory_for(request)() as db:
        rows = (await db.execute(query)).all()
    return [
        DevUserView(
            user_id=user.id,
            name=user.name,
            role=str(user.role),
            shop_id=user.shop_id,
            shop_name=shop_name,
        )
        for user, shop_name in rows
    ]


@router.get("/me", response_model=MeResponse, summary="Who the token says you are")
async def me(request: Request, principal: CurrentUser) -> MeResponse:
    if principal is None:
        return MeResponse(user_id=None, shop_id=None, role=None, authenticated=False)
    user, shop_name = await _names(request, principal.user_id)
    return MeResponse(
        user_id=principal.user_id,
        shop_id=principal.shop_id,
        role=principal.role,
        authenticated=True,
        name=user.name if user else None,
        shop_name=shop_name,
    )
