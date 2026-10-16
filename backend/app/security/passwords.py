"""Password hashing with Argon2id."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2 import exceptions as argon2_exceptions
from argon2.low_level import Type

MIN_PASSWORD_LENGTH = 10

#: Tuned for a home server: ~64 MiB and 3 passes keeps login well under a second
#: on the 4-core LXC the installer provisions, while staying far above the
#: minimum recommended parameters.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=2,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time verification. Never raises on a wrong password."""
    try:
        _hasher.verify(password_hash, password)
    except (
        argon2_exceptions.VerifyMismatchError,
        argon2_exceptions.VerificationError,
        argon2_exceptions.InvalidHashError,
    ):
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash used weaker parameters than the current policy."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except argon2_exceptions.InvalidHashError:
        return True


def validate_password_strength(password: str) -> list[str]:
    """Return a list of human-readable problems; empty means acceptable."""
    problems: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")
    if password.strip() != password:
        problems.append("Password must not start or end with whitespace.")
    if password.lower() in {"password", "sentinel", "admin", "changeme", "1234567890"}:
        problems.append("Password is too common.")
    return problems
