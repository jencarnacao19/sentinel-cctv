"""Redaction for the diagnostics download.

A diagnostics bundle is attached to public issue reports, so it must never carry
an API token or a camera password. Camera credentials mostly arrive embedded in
RTSP URLs (``rtsp://user:pass@host``), which no key-name based filter would
catch, so URLs are scrubbed by pattern as well as by field name.

Kept free of Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "**REDACTED**"

#: rtsp://user:pass@host -> rtsp://**REDACTED**@host. Also covers rtsps, http and
#: https, which is where snapshot and ONVIF URLs hide credentials.
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>[^/\s@]+)@"
)

#: Sentinel API tokens carry a distinctive prefix, so they can be removed
#: wherever they turn up -- including a bare token pasted into an error string.
_SENTINEL_TOKEN = re.compile(r"\bsctv_[A-Za-z0-9_-]{4,}")

#: "password=secret", "token: secret" and "Authorization: Bearer x" in free text.
#: The scheme branch has to come before the generic one or only the word
#: "Bearer" would be replaced and the credential itself would survive.
_KEY_VALUE = re.compile(
    r"(?P<key>\b(?:password|passwd|pwd|secret|token|api[_-]?key|authorization)\b\s*[=:]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'"
    r"|(?:Bearer|Basic|Digest|Token)\s+[^\s,;&)\]}]+"
    r"|[^\s,;&)\]}]+)",
    re.IGNORECASE,
)

#: Field names whose value is dropped whole, whatever it looks like.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "encrypted_password",
        "password",
        "passwd",
        "password_hash",
        "pwd",
        "refresh_token",
        "secret",
        "secret_key",
        "token",
    }
)

#: Guard against a payload with a reference cycle or absurd nesting.
_MAX_DEPTH = 12


def _redact_userinfo(match: re.Match[str]) -> str:
    return f"{match.group('scheme')}{REDACTED}@"


def redact_text(value: str) -> str:
    """Remove URL credentials, key/value secrets and Sentinel tokens from a string."""
    redacted = _URL_CREDENTIALS.sub(_redact_userinfo, value)
    redacted = _KEY_VALUE.sub(lambda match: f"{match.group('key')}{REDACTED}", redacted)
    return _SENTINEL_TOKEN.sub(REDACTED, redacted)


def redact_data(data: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a JSON-like structure.

    A value under a sensitive key is replaced wholesale; every other string still
    goes through :func:`redact_text`, because the dangerous part is usually an
    RTSP URL sitting under an innocent key such as ``main_stream_url``.
    """
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(data, Mapping):
        result: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
                result[str(key)] = None if value is None else REDACTED
            else:
                result[str(key)] = redact_data(value, _depth=_depth + 1)
        return result
    if isinstance(data, str):
        return redact_text(data)
    # str is a Sequence, so it has to be handled above this branch.
    if isinstance(data, Sequence) and not isinstance(data, (bytes, bytearray)):
        return [redact_data(item, _depth=_depth + 1) for item in data]
    return data
