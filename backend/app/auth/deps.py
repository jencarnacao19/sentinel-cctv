"""FastAPI dependencies for authentication and authorisation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from app.auth import service, tokens
from app.config import Settings, get_settings
from app.database.models import ApiToken, ApiTokenScope, User
from app.database.session import get_session

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Authentication required.",
    headers={"WWW-Authenticate": "Bearer"},
)


@dataclass(frozen=True, slots=True)
class Principal:
    """Whoever is making the request: a signed-in user or a machine token."""

    kind: str  # "user" | "api_token"
    id: str
    name: str
    is_admin: bool
    user: User | None = None
    api_token: ApiToken | None = None

    @property
    def is_machine(self) -> bool:
        return self.kind == "api_token"

    @property
    def scope(self) -> str | None:
        return self.api_token.scope if self.api_token is not None else None


def _bearer_credential(connection: HTTPConnection, settings: Settings) -> str | None:
    header = connection.headers.get("Authorization")
    if header:
        scheme, _, credential = header.partition(" ")
        if scheme.lower() == "bearer" and credential.strip():
            return credential.strip()
    # The frontend uses a cookie so the token is never exposed to page scripts.
    cookie = connection.cookies.get(settings.session_cookie_name)
    if cookie:
        return cookie
    # WebSocket clients cannot set headers through the browser API; a query
    # parameter is the standard fallback and is only read for a socket upgrade.
    if connection.scope.get("type") == "websocket":
        query_token = connection.query_params.get("token")
        if query_token:
            return query_token
    return None


async def _user_from_session_token(credential: str, session: AsyncSession) -> User | None:
    try:
        payload = tokens.decode_access_token(credential)
    except tokens.TokenError:
        return None
    user = await session.get(User, payload.subject)
    if user is None or not user.is_active:
        return None
    return user


async def get_session_user(
    connection: HTTPConnection,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User | None:
    """The account signed in on this connection, administrator or viewer.

    Only for what every account may do: watch through the viewer and manage
    its own password and preferences. Everything else goes through a
    principal, which a viewer account never becomes.
    """
    credential = _bearer_credential(connection, settings)
    if not credential or tokens.looks_like_api_token(credential):
        return None
    return await _user_from_session_token(credential, session)


async def get_optional_principal(
    connection: HTTPConnection,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal | None:
    """Resolve the caller from a request *or* a WebSocket handshake.

    ``HTTPConnection`` is the base of both, which is what lets the live-stream
    socket reuse exactly the same authentication as every REST endpoint.
    """
    credential = _bearer_credential(connection, settings)
    if not credential:
        return None

    if tokens.looks_like_api_token(credential):
        api_token = await service.resolve_api_token(session, credential)
        if api_token is None:
            return None
        await session.commit()
        return Principal(
            kind="api_token",
            id=api_token.id,
            name=api_token.name,
            is_admin=False,
            api_token=api_token,
        )

    user = await _user_from_session_token(credential, session)
    if user is None or not user.is_admin:
        # A viewer account signs in to watch, and the viewer has its own
        # authorisation. To every other route it is simply not signed in, so
        # no endpoint written before these accounts existed can serve one.
        return None
    return Principal(kind="user", id=user.id, name=user.username, is_admin=True, user=user)


async def get_current_principal(
    principal: Annotated[Principal | None, Depends(get_optional_principal)],
) -> Principal:
    if principal is None:
        raise _UNAUTHENTICATED
    return principal


async def get_current_user(
    connection: HTTPConnection,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    """Require a human session specifically: an account's own password and profile.

    Any account qualifies, a viewer account included; nothing behind this
    dependency reaches beyond the signed-in account itself.
    """
    credential = _bearer_credential(connection, settings)
    if credential and tokens.looks_like_api_token(credential):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires a signed-in user, not an API token.",
        )
    user = await get_session_user(connection, session, settings)
    if user is None:
        raise _UNAUTHENTICATED
    return user


async def require_admin(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    if principal.is_machine or not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges are required.",
        )
    return principal


async def require_camera_control(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    """Camera operation is explicit for integrations; it never grants administration."""
    if principal.is_machine and principal.scope != ApiTokenScope.CAMERA_CONTROL:
        raise HTTPException(status_code=403, detail="Camera-control permission is required.")
    return principal


async def require_admin_or_camera_control(
    principal: Annotated[Principal, Depends(require_camera_control)],
) -> Principal:
    """Keep existing human administrator requirements on formerly admin-only controls."""
    if not principal.is_machine:
        return await require_admin(principal)
    return principal


async def require_admin_or_integration(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    """Allow integration telemetry while preserving the human administration boundary."""
    if not principal.is_machine:
        return await require_admin(principal)
    return principal


def client_key(connection: HTTPConnection) -> str:
    """Rate-limit bucket key.

    Sentinel is meant to be reached directly on the LAN, so the socket address is
    the honest identifier. ``X-Forwarded-For`` is deliberately ignored: trusting
    it unconditionally would let any client forge its own bucket.
    """
    client = connection.client
    return client.host if client else "unknown"


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminPrincipal = Annotated[Principal, Depends(require_admin)]
CameraControlPrincipal = Annotated[Principal, Depends(require_camera_control)]
AdminCameraControlPrincipal = Annotated[Principal, Depends(require_admin_or_camera_control)]
IntegrationReadPrincipal = Annotated[Principal, Depends(require_admin_or_integration)]
OptionalPrincipal = Annotated[Principal | None, Depends(get_optional_principal)]
SessionUser = Annotated[User | None, Depends(get_session_user)]
