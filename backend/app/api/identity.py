"""Who a request acts for: the login token when there is one, else the body's user_id.

Every route that takes a ``user_id`` goes through here, so the rules are in one place:
with a token, the token decides (a different ``user_id`` in the body is refused, and
the shop must be the user's own); without one, the body's ``user_id`` is used as in
Phases 4 and 5 (allowed only while ``AUTH_REQUIRED=false``).
"""

from typing import TYPE_CHECKING

from app.core.auth import Principal, check_shop
from app.core.exceptions import ForbiddenError, InvalidRequestError

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def acting_user(
    principal: Principal | None, shop_id: str | None, body_user_id: str | None
) -> str | None:
    if principal is None:
        return body_user_id
    if shop_id is not None:
        check_shop(principal, shop_id)
    if principal.role == "admin":
        return body_user_id  # admins act across shops, as the user they name (if any)
    if body_user_id and body_user_id != principal.user_id:
        raise ForbiddenError(
            f"You are logged in as {principal.user_id}; you cannot act as {body_user_id}."
        )
    return principal.user_id


def required_user(principal: Principal | None, body_user_id: str | None) -> str:
    user_id = acting_user(principal, None, body_user_id)
    if not user_id:
        raise InvalidRequestError("Say who decides: log in, or send user_id (for example USR-003).")
    return user_id


def session_factory_for(request: "Request") -> "async_sessionmaker[AsyncSession]":
    """The database sessions this app uses (the agents' runtime's, when it is built)."""
    state = request.app.state
    runtime = getattr(state, "agent_runtime", None)
    if runtime is not None:
        return runtime.session_factory
    override = getattr(state, "session_factory", None)
    if override is not None:
        return override
    from app.models.database import get_session_factory

    return get_session_factory()
