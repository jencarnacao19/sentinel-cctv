"""Parsing and validation of camera stream URLs.

Two jobs, both security-relevant:

1. **Separate credentials from the URL.** Cameras are routinely configured by
   pasting ``rtsp://user:pass@host/stream``. The password must be lifted out and
   encrypted, and the stored URL left clean, so it can be shown in the interface
   and written to a log without leaking anything.

2. **Constrain what Sentinel will connect to.** The URL is user-supplied and is
   handed to FFmpeg, which speaks far more protocols than a camera needs. Only
   an explicit allow-list of schemes is accepted; ``file://`` in particular would
   turn "add a camera" into "read any file the process can reach".

Sentinel is a LAN application and *must* be able to reach private addresses, so
this is deliberately not an SSRF blocklist on the host. The protection is the
scheme allow-list plus the fact that FFmpeg is always invoked with an argument
list, never a shell string.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit, urlunsplit

#: Schemes a camera stream may use. Anything else is refused outright.
ALLOWED_STREAM_SCHEMES = frozenset({"rtsp", "rtsps"})

#: Additional schemes accepted for a still-image snapshot URL.
ALLOWED_SNAPSHOT_SCHEMES = frozenset({"http", "https"})

DEFAULT_PORTS = {"rtsp": 554, "rtsps": 322, "http": 80, "https": 443}

MAX_URL_LENGTH = 2048


class InvalidStreamUrlError(ValueError):
    """Raised when a URL is malformed or uses a scheme Sentinel will not open."""


@dataclass(frozen=True, slots=True)
class ParsedStreamUrl:
    """A stream URL split into its parts, with credentials removed."""

    scheme: str
    host: str
    port: int
    path: str
    username: str | None
    password: str | None

    @property
    def sanitised_url(self) -> str:
        """The URL with no userinfo -- safe to store, display and log."""
        netloc = f"{_format_host(self.host)}:{self.port}"
        return urlunsplit((self.scheme, netloc, self.path, "", ""))

    @property
    def has_credentials(self) -> bool:
        return bool(self.username or self.password)


def _format_host(host: str) -> str:
    """Re-bracket an IPv6 literal so the netloc round-trips."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def parse_stream_url(
    value: str, *, allowed_schemes: frozenset[str] = ALLOWED_STREAM_SCHEMES
) -> ParsedStreamUrl:
    """Validate ``value`` and split it into parts.

    Raises :class:`InvalidStreamUrlError` with a message safe to show the user --
    it never echoes back the credentials it may have contained.
    """
    candidate = value.strip()
    if not candidate:
        raise InvalidStreamUrlError("Stream URL is required.")
    if len(candidate) > MAX_URL_LENGTH:
        raise InvalidStreamUrlError("Stream URL is too long.")
    if any(char in candidate for char in ("\n", "\r", "\x00", " ")):
        raise InvalidStreamUrlError("Stream URL contains invalid characters.")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise InvalidStreamUrlError("Stream URL could not be parsed.") from exc

    scheme = parts.scheme.lower()
    if not scheme:
        raise InvalidStreamUrlError("Stream URL must start with a scheme, for example rtsp://")
    if scheme not in allowed_schemes:
        allowed = ", ".join(sorted(allowed_schemes))
        raise InvalidStreamUrlError(
            f"Scheme '{scheme}' is not allowed. Sentinel accepts: {allowed}."
        )

    try:
        hostname = parts.hostname
        raw_port = parts.port
    except ValueError as exc:
        # urlsplit raises for a non-numeric or out-of-range port.
        raise InvalidStreamUrlError("Stream URL has an invalid port.") from exc

    if not hostname:
        raise InvalidStreamUrlError("Stream URL is missing a host.")

    # `raw_port is None` means no port was given, which is different from an
    # explicit ":0" -- the latter is invalid and must be reported, not silently
    # replaced with the default.
    port = DEFAULT_PORTS.get(scheme, 554) if raw_port is None else raw_port
    if not 1 <= port <= 65535:
        raise InvalidStreamUrlError("Stream URL has an invalid port.")

    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    return ParsedStreamUrl(
        scheme=scheme,
        host=hostname,
        port=port,
        path=path,
        username=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password else None,
    )


def build_stream_url(sanitised_url: str, username: str | None, password: str | None) -> str:
    """Re-attach credentials for handing to FFmpeg or go2rtc.

    The result is a live secret: it goes into an argument list and nowhere else.
    """
    parts = urlsplit(sanitised_url)
    if not username and not password:
        return sanitised_url

    host = _format_host(parts.hostname or "")
    netloc = f"{host}:{parts.port}" if parts.port else host

    # Percent-encode so a password containing '@', ':' or '/' cannot change which
    # host the URL points at.
    userinfo = quote(username or "", safe="")
    if password:
        userinfo = f"{userinfo}:{quote(password, safe='')}"

    return urlunsplit((parts.scheme, f"{userinfo}@{netloc}", parts.path, parts.query, ""))


def normalise_stream_url(
    value: str, *, allowed_schemes: frozenset[str] = ALLOWED_STREAM_SCHEMES
) -> tuple[str, str | None, str | None]:
    """Return ``(clean_url, username, password)`` for a user-supplied URL."""
    parsed = parse_stream_url(value, allowed_schemes=allowed_schemes)
    return parsed.sanitised_url, parsed.username, parsed.password
