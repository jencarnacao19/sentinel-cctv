"""Authentication business logic."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import tokens as token_utils
from app.database.base import utcnow
from app.database.models import ApiToken, ApiTokenScope, User
from app.logging_config import get_logger
from app.security.passwords import (
    hash_password,
    needs_rehash,
    validate_password_strength,
    verify_password,
)

logger = get_logger("API")


class AuthError(Exception):
    """Base class for authentication failures."""


class InvalidCredentialsError(AuthError):
    pass


class WeakPasswordError(AuthError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class AdminAlreadyExistsError(AuthError):
    pass


async def count_users(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(User))
    return int(result.scalar_one())


async def has_any_user(session: AsyncSession) -> bool:
    return await count_users(session) > 0


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    result = await session.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def create_admin(session: AsyncSession, username: str, password: str) -> User:
    """Create the first administrator. Refuses if one already exists.

    This endpoint is reachable unauthenticated during first run, so the guard
    below is what stops a second account being created later.
    """
    if await has_any_user(session):
        raise AdminAlreadyExistsError("An administrator account already exists.")

    problems = validate_password_strength(password)
    if problems:
        raise WeakPasswordError(problems)

    user = User(
        username=username,
        password_hash=hash_password(password),
        is_admin=True,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    logger.info("Administrator account created", extra={"username": username})
    return user


async def authenticate(session: AsyncSession, username: str, password: str) -> User:
    """Verify credentials and record the login."""
    user = await get_user_by_username(session, username)
    if user is None:
        # Hash anyway so a missing user and a wrong password take similar time.
        hash_password(password)
        raise InvalidCredentialsError("Incorrect username or password.")
    if not user.is_active:
        raise InvalidCredentialsError("Incorrect username or password.")
    if not verify_password(user.password_hash, password):
        raise InvalidCredentialsError("Incorrect username or password.")

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = utcnow()
    await session.flush()
    return user


async def change_password(
    session: AsyncSession, user: User, current_password: str, new_password: str
) -> None:
    if not verify_password(user.password_hash, current_password):
        raise InvalidCredentialsError("Current password is incorrect.")
    problems = validate_password_strength(new_password)
    if problems:
        raise WeakPasswordError(problems)
    user.password_hash = hash_password(new_password)
    await session.flush()
    logger.info("Password changed", extra={"username": user.username})


# --- API tokens ---------------------------------------------------------


async def create_api_token(
    session: AsyncSession,
    *,
    name: str,
    scope: ApiTokenScope = ApiTokenScope.READ_ONLY,
    user: User | None = None,
    expires_in_days: int | None = None,
) -> tuple[ApiToken, str]:
    """Create a token and return it together with its one-time plaintext."""
    plaintext, prefix, token_hash = token_utils.generate_api_token()
    token = ApiToken(
        name=name,
        scope=ApiTokenScope(scope),
        prefix=prefix,
        token_hash=token_hash,
        user_id=user.id if user else None,
        expires_at=(utcnow() + timedelta(days=expires_in_days)) if expires_in_days else None,
    )
    session.add(token)
    await session.flush()
    logger.info("API token created", extra={"token_name": name, "token_prefix": prefix})
    return token, plaintext


async def list_api_tokens(session: AsyncSession) -> list[ApiToken]:
    result = await session.execute(select(ApiToken).order_by(ApiToken.created_at.desc()))
    return list(result.scalars())


async def revoke_api_token(session: AsyncSession, token_id: str) -> ApiToken | None:
    token = await session.get(ApiToken, token_id)
    if token is None:
        return None
    if token.revoked_at is None:
        token.revoked_at = utcnow()
        await session.flush()
        logger.info("API token revoked", extra={"token_name": token.name})
    return token


async def resolve_api_token(session: AsyncSession, plaintext: str) -> ApiToken | None:
    """Look up an active token by its plaintext and stamp ``last_used_at``."""
    token_hash = token_utils.hash_api_token(plaintext)
    result = await session.execute(select(ApiToken).where(ApiToken.token_hash == token_hash))
    token = result.scalar_one_or_none()
    if token is None or not token.is_active or token.scope not in set(ApiTokenScope):
        return None

    # Avoid a write on every single request; one-minute granularity is plenty
    # for the "Last used" column and keeps SQLite writes down.
    now = utcnow()
    if token.last_used_at is None or (now - token.last_used_at) > timedelta(minutes=1):
        token.last_used_at = now
        await session.flush()
    return token
