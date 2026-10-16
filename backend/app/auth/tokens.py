"""Signed access tokens for browser sessions, and hashed API tokens for machines.

Two different mechanisms, deliberately:

* **Access tokens** are short-lived, stateless and HMAC-signed with a key derived
  from the local install secret. Verifying one costs no database round trip,
  which matters because the frontend polls nothing but does open a WebSocket and
  fetch snapshots continuously.
* **API tokens** are long-lived and belong to Home Assistant. They must be
  revocable, so only a SHA-256 hash is stored and every use is checked against
  the database.

A full JWT library would add a dependency for a format we never need to
interoperate on; the compact signed envelope below is the same construction with
none of the algorithm-confusion surface (the algorithm is fixed, not read from
the token).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import get_settings

_SESSION_INFO = b"sentinel-cctv/session-tokens/v1"
API_TOKEN_PREFIX = "sctv_"  # noqa: S105 - a namespace marker, not a credential
_API_TOKEN_BYTES = 32


class TokenError(ValueError):
    """Raised when a token is malformed, tampered with or expired."""


@dataclass(frozen=True, slots=True)
class TokenPayload:
    subject: str
    issued_at: datetime
    expires_at: datetime
    token_id: str


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:
        raise TokenError("Malformed token encoding.") from exc


def _signing_key() -> bytes:
    secret = get_settings().load_or_create_secret()
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_SESSION_INFO,
    ).derive(secret)


def create_access_token(
    subject: str,
    *,
    ttl_minutes: int | None = None,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """Return ``(token, expires_at)`` for a user session."""
    settings = get_settings()
    issued = now or datetime.now(UTC)
    ttl = ttl_minutes if ttl_minutes is not None else settings.access_token_ttl_minutes
    expires = issued + timedelta(minutes=ttl)

    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": secrets.token_urlsafe(8),
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(_signing_key(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64encode(signature)}", expires


def decode_access_token(token: str, *, now: datetime | None = None) -> TokenPayload:
    """Verify signature and expiry, returning the payload."""
    parts = token.split(".")
    if len(parts) != 2:
        raise TokenError("Malformed token.")
    body, signature = parts

    expected = hmac.new(_signing_key(), body.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64decode(signature)):
        raise TokenError("Invalid token signature.")

    try:
        payload = json.loads(_b64decode(body))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TokenError("Malformed token payload.") from exc
    if not isinstance(payload, dict):
        raise TokenError("Malformed token payload.")

    try:
        subject = str(payload["sub"])
        issued_at = datetime.fromtimestamp(int(payload["iat"]), tz=UTC)
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
        token_id = str(payload["jti"])
    except (KeyError, TypeError, ValueError, OSError, OverflowError) as exc:
        raise TokenError("Malformed token payload.") from exc

    if expires_at <= (now or datetime.now(UTC)):
        raise TokenError("Token has expired.")

    return TokenPayload(
        subject=subject, issued_at=issued_at, expires_at=expires_at, token_id=token_id
    )


# --- API tokens ---------------------------------------------------------


def generate_api_token() -> tuple[str, str, str]:
    """Return ``(plaintext, prefix, token_hash)``.

    The plaintext is shown to the user exactly once.
    """
    raw = secrets.token_urlsafe(_API_TOKEN_BYTES)
    plaintext = f"{API_TOKEN_PREFIX}{raw}"
    return plaintext, plaintext[: len(API_TOKEN_PREFIX) + 6], hash_api_token(plaintext)


def hash_api_token(plaintext: str) -> str:
    """SHA-256 is correct here: the token is 256 bits of entropy we generated,
    not a low-entropy human password, so a slow KDF buys nothing and would make
    every Home Assistant request expensive."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def looks_like_api_token(value: str) -> bool:
    return value.startswith(API_TOKEN_PREFIX)
