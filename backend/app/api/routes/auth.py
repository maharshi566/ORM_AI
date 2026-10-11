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

A **public demo** (Phase 9) may open the same login on any server for a short list of
demo accounts: ``AUTH_DEMO_USERS=USR-001,USR-003``. Only those users can log in that
way, and never an admin (an admin token reads and decides for every shop). Everyone
else needs a token from the deployment's own login system.
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


def _demo_only(settings: Settings) -> bool:
    """False: any user may log in (development). True: only the demo accounts may.

    Refuses when neither applies, the normal state of a shared server.
    """
    if settings.app_env in DEV_LOGIN_ENVIRONMENTS and settings.auth_dev_login:
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


@router.post("/dev-token", response_model=TokenResponse, summary="A login token for a demo user")
async def dev_token(
    request: Request,
    body: DevTokenRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(login_rate_limit)],
) -> TokenResponse:
    demo_only = _demo_only(settings)
    user, shop_name = await _names(request, body.user_id)
    if user is None or not user.is_active:
        raise NotFoundError(f"No active user {body.user_id}.")
    if demo_only:
        _check_demo_user(user, settings)
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
