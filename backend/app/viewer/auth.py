"""Authorisation for the deliberately small read-only viewer surface.

Viewer access grants exactly the endpoints under ``/viewer`` plus the viewer
WebSocket and never satisfies Sentinel's normal ``CurrentPrincipal`` or
``AdminPrincipal`` dependencies. A viewer account is a signed-in person whose
whole access is this surface: the rest of the API treats it as signed out.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from app.auth.deps import get_session_user
from app.config import Settings, get_settings
from app.database.session import get_session
from app.viewer.cloudflare import valid_cloudflare_assertion

VIEWER_KEY_HEADER = "X-Sentinel-Viewer-Key"
VIEWER_COOKIE_NAME = "sentinel_viewer"
VIEWER_SUBPROTOCOL_PREFIX = "sentinel.viewer."
CF_ACCESS_ASSERTION_HEADER = "Cf-Access-Jwt-Assertion"

# A viewer key is deliberately short. Capping the encoded token prevents an
# attacker from making the handshake allocate an arbitrarily large value.
_MAX_VIEWER_SUBPROTOCOL_LENGTH = 1024

_VIEWER_DENIED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Read-only viewer access is not authorised.",
)


@dataclass(frozen=True, slots=True)
class ViewerAccess:
    """The narrow viewer grant and the mechanism that authorised it."""

    kind: str  # "administrator" | "viewer_account" | "cloudflare_access" | "viewer_key"


def _hostname(connection: HTTPConnection) -> str:
    """Return a lower-case hostname without a port.

    ``URL.hostname`` correctly handles bracketed IPv6 hosts and avoids the
    fragile ``host.split(':')`` pattern.
    """
    return (connection.url.hostname or "").rstrip(".").lower()


async def _trusted_cloudflare_request(connection: HTTPConnection, settings: Settings) -> bool:
    hostname = _hostname(connection)
    trusted = {host.rstrip(".").lower() for host in settings.viewer_trusted_hosts}
    return hostname in trusted and await valid_cloudflare_assertion(
        connection.headers.get(CF_ACCESS_ASSERTION_HEADER), settings
    )


def _decode_viewer_subprotocol(protocol: str) -> str | None:
    if not protocol.startswith(VIEWER_SUBPROTOCOL_PREFIX):
        return None
    encoded = protocol.removeprefix(VIEWER_SUBPROTOCOL_PREFIX)
    if not encoded or len(encoded) > _MAX_VIEWER_SUBPROTOCOL_LENGTH:
        return None
    try:
        padding = "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(encoded + padding).decode("utf-8").strip()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def _viewer_subprotocols(connection: HTTPConnection) -> list[tuple[str, str]]:
    requested = connection.headers.get("sec-websocket-protocol", "")
    matches: list[tuple[str, str]] = []
    for raw in requested.split(","):
        protocol = raw.strip()
        decoded = _decode_viewer_subprotocol(protocol)
        if decoded:
            matches.append((protocol, decoded))
    return matches


def _supplied_viewer_keys(connection: HTTPConnection) -> list[str]:
    supplied: list[str] = []
    header = connection.headers.get(VIEWER_KEY_HEADER)
    if header:
        supplied.append(header.strip())
    supplied.extend(decoded for _, decoded in _viewer_subprotocols(connection))
    cookie = connection.cookies.get(VIEWER_COOKIE_NAME)
    if cookie:
        supplied.append(cookie.strip())
    return supplied


def _valid_viewer_key(
    connection: HTTPConnection,
    settings: Settings,
    supplied_key: str | None = None,
) -> bool:
    configured = settings.viewer_key
    if configured is None:
        return False
    candidates = [supplied_key] if supplied_key else _supplied_viewer_keys(connection)
    expected = configured.get_secret_value()
    return any(secrets.compare_digest(expected, candidate) for candidate in candidates)


def matching_viewer_subprotocol(
    connection: HTTPConnection,
    settings: Settings,
) -> str | None:
    """Return the requested protocol that proves viewer access, if present.

    A browser WebSocket cannot attach a custom header. Subprotocols are the
    standards-based handshake field browsers do expose, and unlike query
    parameters they are not written to the request URL or normal access logs.
    """
    if settings.viewer_key is None:
        return None
    expected = settings.viewer_key.get_secret_value()
    for protocol, candidate in _viewer_subprotocols(connection):
        if secrets.compare_digest(expected, candidate):
            return protocol
    return None


async def authorise_viewer(
    connection: HTTPConnection,
    session: AsyncSession,
    settings: Settings,
    supplied_key: str | None = None,
) -> ViewerAccess:
    """Authorise a viewer request without granting application privileges."""
    # Machine tokens are never accounts here: they use the scoped camera API,
    # and treating them as operators would bypass those scopes through viewer
    # PTZ and permanent exports.
    account = await get_session_user(connection, session, settings)
    if account is not None:
        # Accounts are local people signed in with a password, so they do not
        # depend on the remote viewer being enabled.
        return ViewerAccess(kind="administrator" if account.is_admin else "viewer_account")

    if not settings.viewer_enabled:
        raise _VIEWER_DENIED
    if _valid_viewer_key(connection, settings, supplied_key):
        return ViewerAccess(kind="viewer_key")
    if await _trusted_cloudflare_request(connection, settings):
        return ViewerAccess(kind="cloudflare_access")
    raise _VIEWER_DENIED


async def require_viewer(
    connection: HTTPConnection,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ViewerAccess:
    return await authorise_viewer(connection, session, settings)


CurrentViewer = Annotated[ViewerAccess, Depends(require_viewer)]
