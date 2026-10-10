"""Login tokens (Phase 6).

``POST /api/auth/dev-token`` gives a token for one of the demo users, so the API and
the frontend can be tried with real logins. It works only when ``APP_ENV`` is
``local`` or ``test`` and ``AUTH_DEV_LOGIN=true``. A real deployment issues tokens
from its own login system (signed with ``AUTH_SECRET``, see app/core/auth.py).

Send the token as ``Authorization: Bearer <token>``. In http://localhost:8000/docs,
click **Authorize** (top right) and paste it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.identity import session_factory_for
from app.config.settings import Settings, get_settings
from app.core.auth import CurrentUser, Principal, create_token
from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.rate_limit import login_rate_limit
from app.models import User
from app.models.schemas import DevTokenRequest, MeResponse, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


# Only on a developer's machine and in tests: on any shared server (staging,
# production) anyone could otherwise log in as anyone, the admin included.
DEV_LOGIN_ENVIRONMENTS = {"local", "test"}


@router.post("/dev-token", response_model=TokenResponse, summary="A login token for a demo user")
async def dev_token(
    request: Request,
    body: DevTokenRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(login_rate_limit)],
) -> TokenResponse:
    if settings.app_env not in DEV_LOGIN_ENVIRONMENTS or not settings.auth_dev_login:
        raise ForbiddenError(
            "Development logins are switched off on this server (only APP_ENV=local or "
            "test, with AUTH_DEV_LOGIN=true)."
        )
    async with session_factory_for(request)() as db:
        user = await db.get(User, body.user_id)
    if user is None or not user.is_active:
        raise NotFoundError(f"No active user {body.user_id}.")
    principal = Principal(user_id=user.id, shop_id=user.shop_id, role=str(user.role))  # type: ignore[arg-type]
    return TokenResponse(
        access_token=create_token(principal, settings),
        expires_in=settings.auth_token_hours * 3600,
        user_id=user.id,
        shop_id=user.shop_id,
        role=str(user.role),
    )


@router.get("/me", response_model=MeResponse, summary="Who the token says you are")
async def me(principal: CurrentUser) -> MeResponse:
    if principal is None:
        return MeResponse(user_id=None, shop_id=None, role=None, authenticated=False)
    return MeResponse(
        user_id=principal.user_id,
        shop_id=principal.shop_id,
        role=principal.role,
        authenticated=True,
    )
