"""Real logins: email + password, and a one-time code sent to the user's phone.

Both end in the same signed token as before (app/core/auth.py), so everything after
the login is unchanged. The rules, kept in one place:

* **Passwords** are Argon2id hashes (app/core/passwords.py). After
  ``MAX_FAILED_LOGINS`` wrong passwords in a row the account is locked for
  ``LOCKOUT_MINUTES``. A wrong email and a wrong password get the same answer, so
  the login form does not reveal who has an account.
* **One-time codes** are six digits, valid for ``CODE_TTL_MINUTES``, good for one
  login and at most ``CODE_MAX_ATTEMPTS`` guesses. Only a keyed hash is stored. A new
  code can be asked for after ``CODE_RESEND_SECONDS``, and at most
  ``CODE_MAX_PER_HOUR`` times an hour. Asking for a code always gets the same answer,
  whether or not the number belongs to an account.
* **Changing a password** raises the user's ``token_version``: every older token
  stops working, so a stolen session ends with the password change.
* **Who may add users**: an owner adds staff to their own shop; an admin adds anyone.

Codes reach the phone through a ``CodeSender``: WhatsApp (an authentication template,
docs/whatsapp.md), or the console (the backend log) on a developer's machine.
"""

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.core.auth import Principal, UnauthorizedError, signing_key
from app.core.exceptions import AppError, ConflictError, ForbiddenError, InvalidRequestError
from app.core.logging import get_logger
from app.core.passwords import hash_password, needs_rehash, password_problems, verify_password
from app.core.phones import is_dialable, normalize_phone
from app.integrations.whatsapp import Template, WhatsAppClient, WhatsAppError
from app.models import LoginCode, Shop, User

logger = get_logger(__name__)

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
CODE_DIGITS = 6
CODE_TTL_MINUTES = 10
CODE_MAX_ATTEMPTS = 5
CODE_RESEND_SECONDS = 60
CODE_MAX_PER_HOUR = 5

WRONG_PASSWORD = "Wrong email or password."  # noqa: S105 - a message, not a password
WRONG_CODE = "The code is wrong or has expired. Ask for a new one."
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


class LockedError(AppError):
    status_code = 429
    code = "account_locked"


def _utc(value: datetime | None) -> datetime | None:
    """SQLite gives naive datetimes back; the app stores UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def normalize_email(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    email = raw.strip().lower()
    if len(email) > 254 or not EMAIL.match(email):
        raise InvalidRequestError("That does not look like an email address.")
    return email


# ------------------------------------------------------------------ code senders


class CodeSender(Protocol):
    channel: str

    async def send_code(self, phone_e164: str, code: str) -> None: ...


class ConsoleCodeSender:
    """Writes the code to the backend log. Development and tests only (the settings
    refuse OTP_CHANNEL=console in production)."""

    channel = "console"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []  # (phone, code), for tests

    async def send_code(self, phone_e164: str, code: str) -> None:
        self.sent.append((phone_e164, code))
        logger.warning("login code (development only)", phone=phone_e164, code=code)


class WhatsAppCodeSender:
    """Sends the code as WhatsApp's authentication template ("123456 is your
    verification code", with a Copy code button)."""

    channel = "whatsapp"

    def __init__(self, settings: Settings, client: WhatsAppClient | None = None) -> None:
        self.settings = settings
        self.client = client or WhatsAppClient(settings)

    async def send_code(self, phone_e164: str, code: str) -> None:
        test = self.settings.whatsapp_test_recipient.strip()
        target = normalize_phone(test) if test else phone_e164
        if not is_dialable(target):
            raise WhatsAppError("This number cannot receive WhatsApp messages.", retryable=False)
        template = Template(
            self.settings.whatsapp_template_login_code,
            self.settings.whatsapp_language,
            body=(code,),
            copy_code=code,
        )
        await self.client.send_template(target, template)  # type: ignore[arg-type]


def code_sender_for(settings: Settings) -> CodeSender | None:
    """The sender OTP_CHANNEL asks for; None when one-time codes are off."""
    mode = settings.otp_mode
    if mode == "whatsapp":
        return WhatsAppCodeSender(settings)
    if mode == "console":
        return ConsoleCodeSender()
    return None


# ------------------------------------------------------------------ passwords


async def password_login(db: AsyncSession, email: str, password: str, now: datetime) -> User:
    """The user these credentials belong to. Raises UnauthorizedError or LockedError."""
    try:
        address = normalize_email(email)
    except InvalidRequestError:
        address = None
    user = (
        await db.scalar(select(User).where(User.email == address, User.is_active.is_(True)))
        if address
        else None
    )
    if user is None:
        await verify_password(None, password)  # the same work as a real check
        raise UnauthorizedError(WRONG_PASSWORD)
    locked_until = _utc(user.locked_until)
    if locked_until and locked_until > now:
        minutes = max(1, int((locked_until - now).total_seconds() // 60) + 1)
        raise LockedError(
            f"Too many wrong passwords. Try again in {minutes} minutes, or log in with a "
            "code sent to your phone.",
            details={"retry_after_seconds": int((locked_until - now).total_seconds())},
        )
    if not await verify_password(user.password_hash, password):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.failed_logins = 0
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            await db.commit()
            raise LockedError(
                f"Too many wrong passwords. The account is locked for {LOCKOUT_MINUTES} minutes.",
                details={"retry_after_seconds": LOCKOUT_MINUTES * 60},
            )
        await db.commit()
        raise UnauthorizedError(WRONG_PASSWORD)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    if user.password_hash and needs_rehash(user.password_hash):
        user.password_hash = await hash_password(password)
    await db.commit()
    return user


async def change_password(
    db: AsyncSession, user_id: str, current: str, new: str, now: datetime
) -> User:
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("This login is no longer valid. Log in again.")
    if user.password_hash and not await verify_password(user.password_hash, current):
        raise UnauthorizedError("The current password is wrong.")
    await _set_password(user, new)
    await db.commit()
    return user


async def _set_password(user: User, new: str) -> None:
    problems = password_problems(new, email=user.email, phone=user.phone_e164)
    if problems:
        raise InvalidRequestError(" ".join(problems), details={"problems": problems})
    user.password_hash = await hash_password(new)
    user.failed_logins = 0
    user.locked_until = None
    user.token_version = (user.token_version or 0) + 1  # older tokens stop working


# ------------------------------------------------------------------ one-time codes


def _code_hash(settings: Settings, code_id: str, code: str) -> str:
    message = f"login-code:{code_id}:{code}".encode()
    return hmac.new(signing_key(settings), message, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class CodeRequest:
    """What the caller is told: the same whether or not the number has an account."""

    channel: str
    expires_in: int = CODE_TTL_MINUTES * 60
    resend_after: int = CODE_RESEND_SECONDS


async def request_login_code(
    db: AsyncSession, phone: str, sender: CodeSender, settings: Settings, now: datetime
) -> CodeRequest:
    answer = CodeRequest(channel=sender.channel)
    number = normalize_phone(phone)
    if number is None:
        raise InvalidRequestError("That does not look like a phone number.")
    user = await db.scalar(select(User).where(User.phone_e164 == number, User.is_active.is_(True)))
    if user is None:
        logger.info("login code asked for an unknown number")
        return answer
    recent = (
        await db.scalars(
            select(LoginCode.created_at).where(
                LoginCode.user_id == user.id, LoginCode.created_at >= now - timedelta(hours=1)
            )
        )
    ).all()
    newest = max((_utc(t) for t in recent), default=None)
    if len(recent) >= CODE_MAX_PER_HOUR or (
        newest and (now - newest).total_seconds() < CODE_RESEND_SECONDS
    ):
        logger.info("login code throttled", user_id=user.id, last_hour=len(recent))
        return answer
    code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
    row = LoginCode(
        id=str(uuid.uuid4()),
        user_id=user.id,
        channel=sender.channel,
        code_hash="",
        attempts=0,
        expires_at=now + timedelta(minutes=CODE_TTL_MINUTES),
        created_at=now,
    )
    row.code_hash = _code_hash(settings, row.id, code)
    db.add(row)
    await db.commit()
    try:
        await sender.send_code(number, code)
    except WhatsAppError as exc:
        # The user sees the same answer; the log says why, without the code.
        logger.error("login code not sent", user_id=user.id, error=str(exc))
    return answer


async def verify_login_code(
    db: AsyncSession, phone: str, code: str, settings: Settings, now: datetime
) -> User:
    number = normalize_phone(phone)
    user = (
        await db.scalar(select(User).where(User.phone_e164 == number, User.is_active.is_(True)))
        if number
        else None
    )
    if user is None or not code.isdigit():
        raise UnauthorizedError(WRONG_CODE)
    row = await db.scalar(
        select(LoginCode)
        .where(LoginCode.user_id == user.id, LoginCode.consumed_at.is_(None))
        .order_by(LoginCode.created_at.desc())
        .limit(1)
    )
    expires = _utc(row.expires_at) if row else None
    if row is None or expires is None or expires <= now or row.attempts >= CODE_MAX_ATTEMPTS:
        raise UnauthorizedError(WRONG_CODE)
    row.attempts += 1
    if not hmac.compare_digest(row.code_hash, _code_hash(settings, row.id, code.strip())):
        if row.attempts >= CODE_MAX_ATTEMPTS:
            row.consumed_at = now  # no more guesses on this code
        await db.commit()
        raise UnauthorizedError(WRONG_CODE)
    row.consumed_at = now
    user.last_login_at = now
    await db.commit()
    return user


# ------------------------------------------------------------------ managing users


async def _next_user_id(db: AsyncSession) -> str:
    ids = (await db.scalars(select(User.id).where(User.id.like("USR-%")))).all()
    numbers = [int(i[4:]) for i in ids if i[4:].isdigit()]
    width = max(3, len(str(max(numbers, default=0) + 1)))
    return f"USR-{max(numbers, default=0) + 1:0{width}d}"


async def create_user(
    db: AsyncSession,
    creator: Principal | None,
    *,
    name: str,
    role: str,
    shop_id: str | None,
    email: str | None = None,
    phone: str | None = None,
    password: str | None = None,
) -> User:
    """Add a user. ``creator`` None means the command line (scripts/create_user.py)."""
    owner_adds_own_staff = (
        creator is not None
        and creator.role == "owner"
        and role == "staff"
        and shop_id == creator.shop_id
    )
    if creator is not None and creator.role != "admin" and not owner_adds_own_staff:
        raise ForbiddenError("An owner can add staff to their own shop; nothing else.")
    if role not in {"owner", "staff", "admin"}:
        raise InvalidRequestError("role must be owner, staff or admin.")
    if role == "admin":
        shop_id = None
    elif not shop_id or await db.get(Shop, shop_id) is None:
        raise InvalidRequestError(f"No shop {shop_id}.")
    address = normalize_email(email)
    number = normalize_phone(phone) if phone else None
    if phone and number is None:
        raise InvalidRequestError("That does not look like a phone number.")
    if not address and not number:
        raise InvalidRequestError("Give an email address or a phone number to log in with.")
    if address and await db.scalar(select(func.count()).where(User.email == address)):
        raise ConflictError("Another user already has this email address.")
    if number and await db.scalar(select(func.count()).where(User.phone_e164 == number)):
        raise ConflictError("Another user already has this phone number.")
    user = User(
        id=await _next_user_id(db),
        shop_id=shop_id,
        name=name.strip(),
        role=role,
        phone=number,
        phone_e164=number,
        email=address,
        is_active=True,
        failed_logins=0,
        token_version=0,
    )
    if password:
        await _set_password(user, password)
        user.token_version = 0
    db.add(user)
    await db.commit()
    return user


async def update_user(
    db: AsyncSession,
    manager: Principal | None,
    user_id: str,
    *,
    is_active: bool | None = None,
    password: str | None = None,
) -> User:
    """Switch a user off or on, or set a new password (both end their sessions)."""
    user = await db.get(User, user_id)
    if user is None:
        raise InvalidRequestError(f"No user {user_id}.")
    if manager is not None and manager.role != "admin":
        allowed = (
            manager.role == "owner"
            and user.shop_id == manager.shop_id
            and str(user.role) == "staff"
        )
        if not allowed:
            raise ForbiddenError("An owner can change the staff of their own shop only.")
        if user.id == manager.user_id:
            raise ForbiddenError("Use your own password page to change your password.")
    if is_active is not None and is_active != user.is_active:
        user.is_active = is_active
        user.token_version = (user.token_version or 0) + 1
    if password:
        await _set_password(user, password)
    await db.commit()
    return user
