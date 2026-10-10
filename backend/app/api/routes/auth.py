"""Login tokens (Phase 6).

``POST /api/auth/dev-token`` gives a token for one of the demo users, so the API and
the frontend can be tried with real logins. It works only when ``APP_ENV`` is
``local`` or ``test`` and ``AUTH_DEV_LOGIN=true``. A real deployment issues tokens
from its own login system (signed with ``AUTH_SECRET``, see app/core/auth.py).

Send the token as ``Authorization: Bearer <token>``. In http://localhost:8000/docs,
click **Authorize** (top right) and paste it.

``GET /api/auth/dev-users`` lists the demo users the development login accepts (the
frontend's login page shows them). It follows the same rule as the development login:
local and test only.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.api.identity import session_factory_for
from app.config.settings import Settings, get_settings
from app.core.auth import CurrentUser, Principal, create_token
from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.rate_limit import login_rate_limit
from app.models import Shop, User
from app.models.schemas import DevTokenRequest, DevUserView, MeResponse, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


# Only on a developer's machine and in tests: on any shared server (staging,
# production) anyone could otherwise log in as anyone, the admin included.
DEV_LOGIN_ENVIRONMENTS = {"local", "test"}


def _check_dev_login(settings: Settings) -> None:
    if settings.app_env not in DEV_LOGIN_ENVIRONMENTS or not settings.auth_dev_login:
        raise ForbiddenError(
            "Development logins are switched off on this server (only APP_ENV=local or "
            "test, with AUTH_DEV_LOGIN=true)."
        )


async def _names(request: Request, user_id: str) -> tuple[User | None, str | None]:
    """The user row and their shop's name."""
    async with session_factory_for(request)() as db:
        user = await db.get(User, user_id)
        shop = await db.get(Shop, user.shop_id) if user and user.shop_id else None
    return user, shop.name if shop else None


@router.post("/dev-token", response_model=TokenResponse, summary="A login token for a demo user")
async def dev_token(
    request: Request,
    body: DevTokenRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(login_rate_limit)],
) -> TokenResponse:
    _check_dev_login(settings)
    user, shop_name = await _names(request, body.user_id)
    if user is None or not user.is_active:
        raise NotFoundError(f"No active user {body.user_id}.")
    principal = Principal(user_id=user.id, shop_id=user.shop_id, role=str(user.role))  # type: ignore[arg-type]
    return TokenResponse(
        access_token=create_token(principal, settings),
        expires_in=settings.auth_token_hours * 3600,
        user_id=user.id,
        shop_id=user.shop_id,
        role=str(user.role),
        name=user.name,
        shop_name=shop_name,
    )


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
    _check_dev_login(settings)
    async with session_factory_for(request)() as db:
        rows = (
            await db.execute(
                select(User, Shop.name)
                .join(Shop, Shop.id == User.shop_id, isouter=True)
                .where(User.is_active.is_(True))
                .order_by(User.shop_id.is_(None), User.shop_id, User.id)
            )
        ).all()
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
