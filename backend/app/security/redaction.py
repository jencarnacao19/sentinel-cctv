"""Redaction of secrets from log output and diagnostics.

Camera credentials routinely appear inside RTSP URLs. Nothing that reaches a log
file, a diagnostics bundle or an API error message may contain them.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "***"

#: rtsp://user:pass@host -> rtsp://***:***@host. Also covers rtsps/http/https,
#: which show up in ONVIF and snapshot URLs.
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>[^/\s@]+)@",
)

#: Bare "password=secret" / "token: secret" pairs in query strings and configs.
#: The value alternation puts quoted strings first, then an auth scheme followed
#: by its credential -- without that branch, "Authorization: Bearer <token>"
#: would redact only the word "Bearer" and leak the token itself.
_KEY_VALUE = re.compile(
    r"(?P<key>\b(?:password|passwd|pwd|secret|token|api[_-]?key|authorization)\b\s*[=:]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'"
    r"|(?:Bearer|Basic|Digest|Token)\s+[^\s,;&)\]}]+"
    r"|[^\s,;&)\]}]+)",
    re.IGNORECASE,
)

#: Sentinel API tokens carry a distinctive prefix, so they can be scrubbed
#: wherever they appear -- including places no key/value rule would catch, such
#: as a bare token pasted into an error message or a go2rtc command line.
_SENTINEL_TOKEN = re.compile(r"\bsctv_[A-Za-z0-9_-]{8,}")

#: Sensitive field names whose values must never be serialised.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "encrypted_password",
        "password_hash",
        "secret_key",
        "access_token",
        "refresh_token",
    }
)


def _redact_userinfo(match: re.Match[str]) -> str:
    userinfo = match.group("userinfo")
    scheme = match.group("scheme")
    if ":" in userinfo:
        return f"{scheme}{REDACTED}:{REDACTED}@"
    return f"{scheme}{REDACTED}@"


def redact_url(value: str) -> str:
    """Replace the userinfo component of any URL in ``value``."""
    return _URL_CREDENTIALS.sub(_redact_userinfo, value)


def redact_text(value: str) -> str:
    """Redact URL credentials, key/value secret pairs and Sentinel API tokens."""
    redacted = redact_url(value)
    redacted = _KEY_VALUE.sub(lambda m: f"{m.group('key')}{REDACTED}", redacted)
    return _SENTINEL_TOKEN.sub(REDACTED, redacted)


def redact_mapping(data: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a JSON-like structure.

    Values under a sensitive key are replaced wholesale; every other string is
    passed through :func:`redact_text` so embedded RTSP URLs are still caught.
    """
    if _depth > 12:  # defensive: stop pathological nesting
        return data
    if isinstance(data, dict):
        result: dict[Any, Any] = {}
        for key, value in data.items():
            if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
                result[key] = REDACTED if value is not None else None
            else:
                result[key] = redact_mapping(value, _depth=_depth + 1)
        return result
    if isinstance(data, (list, tuple)):
        redacted = [redact_mapping(item, _depth=_depth + 1) for item in data]
        return type(data)(redacted) if isinstance(data, tuple) else redacted
    if isinstance(data, str):
        return redact_text(data)
    return data
