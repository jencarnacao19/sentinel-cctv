"""Same-origin checks for browser requests that CORS does not cover.

A WebSocket handshake is not subject to CORS: a page on any site can open a
socket to Sentinel, and the browser attaches Sentinel's cookies to it. The
viewer cookie is ``SameSite=None`` so the Home Assistant iframe keeps working,
which means another site could otherwise open the live-stream socket with a
visitor's cookie and read the video. Browsers always send ``Origin`` on that
handshake, so comparing it with the host the socket was opened on closes the
gap without affecting Home Assistant or scripts, which send no ``Origin``.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.requests import HTTPConnection

from app.config import Settings

#: A socket arrives as ws/wss while the page that opened it was http/https.
_HTTP_SCHEME_FOR = {"ws": "http", "wss": "https"}


def request_origin(connection: HTTPConnection) -> str:
    """Return the public origin seen by the client, accounting for one proxy hop."""

    forwarded_proto = connection.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    scheme = forwarded_proto or connection.url.scheme
    scheme = _HTTP_SCHEME_FOR.get(scheme, scheme)
    forwarded_host = connection.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    host = forwarded_host or connection.headers.get("host", "")
    return f"{scheme}://{host}".rstrip("/")


def websocket_origin_allowed(connection: HTTPConnection, settings: Settings) -> bool:
    """Whether a WebSocket handshake may go ahead.

    A client without ``Origin`` is not a browser and cannot be carrying a
    visitor's cookie, so it is allowed. A browser's ``Origin`` must name the
    host the socket was opened on, or one of the configured CORS origins. Only
    the host and port are compared: behind a TLS-terminating proxy the socket
    can reach Sentinel as plain ``ws`` while the page itself was ``https``.
    """

    origin = connection.headers.get("origin", "").strip().rstrip("/")
    if not origin:
        return True
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if origin in {configured.rstrip("/") for configured in settings.cors_origins}:
        return True
    own = urlsplit(request_origin(connection))
    return bool(own.netloc) and parsed.netloc.lower() == own.netloc.lower()
