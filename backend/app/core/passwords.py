"""Passwords: Argon2id hashes and a short, modern policy.

Hashing uses argon2-cffi's defaults (RFC 9106's second recommended profile: 64 MiB,
3 passes), which take tens of milliseconds, so it runs in a worker thread to keep the
event loop free. A hash made with older parameters is upgraded at the next login.

The policy follows NIST SP 800-63B: at least 10 characters, no forced mix of symbols,
and refuse passwords that are common or contain the account's own email or phone.
"""

import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 10
MAX_LENGTH = 128  # long enough for passphrases, short enough to bound the hashing work
# The most common passwords that pass the length rule (from public breach lists).
COMMON = frozenset(
    {
        "1234567890", "0987654321", "1111111111", "0000000000", "1234512345",
        "qwertyuiop", "password12", "password123", "password1234", "iloveyou12",
        "abcdefghij", "abcd123456", "qwerty1234", "1q2w3e4r5t", "welcome123",
        "admin12345", "letmein123", "india12345", "kirana1234", "shop123456",
    }
)  # fmt: skip

_hasher = PasswordHasher()


def password_problems(
    password: str, *, email: str | None = None, phone: str | None = None
) -> list[str]:
    """Why a new password is refused, in plain sentences (empty: it is fine)."""
    problems = []
    if len(password) < MIN_LENGTH:
        problems.append(f"Use at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        problems.append(f"Use at most {MAX_LENGTH} characters.")
    lowered = password.lower()
    if lowered in COMMON or len(set(password)) <= 2:
        problems.append("This password is too common; choose something less guessable.")
    local_part = (email or "").split("@")[0].lower()
    if len(local_part) >= 4 and local_part in lowered:
        problems.append("Do not use your email address in the password.")
    digits = "".join(ch for ch in phone or "" if ch.isdigit())
    if len(digits) >= 10 and digits[-10:] in password:
        problems.append("Do not use your phone number in the password.")
    return problems


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password_hash: str | None, password: str) -> bool:
    """True when the password matches. Without a stored hash, still does the work of
    one check, so timing does not tell which accounts have a password."""
    if not password_hash:
        await asyncio.to_thread(_verify, _DUMMY_HASH, password)
        return False
    return await asyncio.to_thread(_verify, password_hash, password)


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


_DUMMY_HASH = _hasher.hash("an unguessable placeholder that no account uses")
