"""The shop's team: who can log in (after Phase 9).

* ``GET /api/users``: the users an owner manages (their shop), or every user (admin).
* ``POST /api/users``: add a user. An owner adds staff to their own shop; an admin
  adds anyone. Give an email (password login), a phone (one-time codes), or both.
* ``PATCH /api/users/{user_id}``: switch a user off or on, or set a new password.
  Both end that user's sessions at once.

Staff cannot manage users. Without a token (local development only, as everywhere
else) the caller counts as an admin.
"""

from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.api.identity import session_factory_for
from app.core.auth import CurrentUser
from app.core.exceptions import ForbiddenError
from app.models import User
from app.models.schemas import UserCreateRequest, UserUpdateRequest, UserView
from app.services import auth_service
from app.tools.helpers import mask_phone

router = APIRouter(prefix="/users", tags=["users"])


def _view(user: User) -> UserView:
    return UserView(
        user_id=user.id,
        name=user.name,
        role=str(user.role),
        shop_id=user.shop_id,
        email=user.email,
        phone=mask_phone(user.phone_e164 or user.phone),
        is_active=bool(user.is_active),
        has_password=bool(user.password_hash),
        last_login_at=user.last_login_at,
    )


@router.get("", response_model=list[UserView], summary="The users you manage")
async def list_users(
    request: Request,
    principal: CurrentUser,
    shop_id: Annotated[str | None, Query(max_length=16)] = None,
) -> list[UserView]:
    query = select(User).order_by(User.shop_id.is_(None), User.shop_id, User.id)
    if principal is not None and principal.role != "admin":
        if principal.role != "owner":
            raise ForbiddenError("Only the shop owner can see and manage the team.")
        query = query.where(User.shop_id == principal.shop_id)
    elif shop_id:
        query = query.where(User.shop_id == shop_id)
    async with session_factory_for(request)() as db:
        users = (await db.scalars(query.limit(500))).all()
    return [_view(u) for u in users]


@router.post("", response_model=UserView, status_code=201, summary="Add a user")
async def create_user(
    request: Request, body: UserCreateRequest, principal: CurrentUser
) -> UserView:
    shop_id = body.shop_id
    if principal is not None and principal.role == "owner" and not shop_id:
        shop_id = principal.shop_id
    async with session_factory_for(request)() as db:
        user = await auth_service.create_user(
            db,
            principal,
            name=body.name,
            role=body.role,
            shop_id=shop_id,
            email=body.email,
            phone=body.phone,
            password=body.password,
        )
    return _view(user)


@router.patch(
    "/{user_id}", response_model=UserView, summary="Switch a user off, or reset a password"
)
async def update_user(
    request: Request, user_id: str, body: UserUpdateRequest, principal: CurrentUser
) -> UserView:
    async with session_factory_for(request)() as db:
        user = await auth_service.update_user(
            db, principal, user_id, is_active=body.is_active, password=body.password
        )
    return _view(user)
